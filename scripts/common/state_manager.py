"""Per-app review state, persisted on R2 (one object per app+provider).

State is the operational bookkeeping (posted_ids, the reviews map, the baseline,
reply flags/hashes) — NOT the dashboard content. It lives on the private R2
bucket at:

    {R2_DATA_PREFIX}/state/{appcode}/appstore.json
    {R2_DATA_PREFIX}/state/{appcode}/playstore.json

one object per provider, created lazily the first time an app writes (an
Android-only app only ever writes playstore.json). A brand-new app has no object
yet, so load_state returns an empty state and the first run baselines.

Concurrency: the sync job and the reply workflow can both write the same app's
state object. Writes use R2 **conditional puts** (If-Match on the ETag from the
last read; If-None-Match on first create). On a 412 (another writer got there
first) we re-read, merge_states(remote, mine), and retry — the same reconciliation
the old git commit did, now optimistic-locked on R2 instead of merged in git.

The in-memory shape and helpers (upsert_review, mark_posted, the newest-first
posted_ids ordering, the v1->v2 migration) are unchanged — only persistence moved
from git to R2.
"""

import json
import logging
import os
from datetime import datetime, timezone


LOG = logging.getLogger(__name__)

STATE_VERSION = 2
_MAX_SAVE_ATTEMPTS = 6

# ETag of each state object as last read/written, keyed by R2 key, so save_state
# can do a conditional put without re-reading on the common (no-conflict) path.
_etags: dict[str, str] = {}


# --------------------------------------------------------------------------- #
# R2 wiring (publish owns the client; imported lazily to avoid a circular
# import — publish imports now_iso from this module).
# --------------------------------------------------------------------------- #
def _client():
    from common import publish

    return publish._r2_client()


def _bucket() -> str:
    from common import publish

    return publish._require_env("R2_BUCKET")


def _state_key(provider: str) -> str:
    """R2 object key for an app+provider state, under the data prefix.

    APP_CODE scopes the key so apps never share state. When APP_CODE is unset
    (legacy/manual runs) the old flat name is used so nothing collides.
    """
    from common import publish

    prefix = publish._require_env("R2_DATA_PREFIX").strip("/")
    app_code = os.environ.get("APP_CODE", "").strip()
    if app_code:
        return f"{prefix}/state/{app_code}/{provider}.json"
    return f"{prefix}/state/{provider}_reviews.json"


def _is_missing(exc) -> bool:
    """True when an R2 error means 'object does not exist' (first run)."""
    from botocore.exceptions import ClientError

    if isinstance(exc, ClientError):
        err = exc.response.get("Error", {})
        return err.get("Code") in {"NoSuchKey", "NotFound", "404"}
    return exc.__class__.__name__ == "NoSuchKey"


def _is_precondition_failed(exc) -> bool:
    """True when a conditional put was rejected (another writer won the race)."""
    from botocore.exceptions import ClientError

    if not isinstance(exc, ClientError):
        return False
    err = exc.response.get("Error", {})
    status = exc.response.get("ResponseMetadata", {}).get("HTTPStatusCode")
    return err.get("Code") == "PreconditionFailed" or status == 412


# --------------------------------------------------------------------------- #
# State document shape / migration (unchanged from the git version)
# --------------------------------------------------------------------------- #
def _empty_state() -> dict:
    return {
        "state_version": STATE_VERSION,
        "last_review_id": None,
        "last_checked": None,
        "posted_ids": [],
        "reviews": {},
    }


def _parse_and_migrate(body: str, where: str) -> dict:
    try:
        state = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"State object is invalid JSON: {where}. Restore it or remove it deliberately.") from exc

    if not isinstance(state, dict):
        raise RuntimeError(f"State object must contain a JSON object: {where}")
    if "reviews" in state and not isinstance(state["reviews"], dict):
        raise RuntimeError(f"State 'reviews' must be an object: {where}")
    for review_id, entry in state.get("reviews", {}).items():
        if not isinstance(review_id, str) or not isinstance(entry, dict):
            raise RuntimeError(f"State review entry {review_id!r} must be an object: {where}")

    state.setdefault("last_review_id", None)
    state.setdefault("last_checked", None)
    state.setdefault("reviews", {})
    state.setdefault("posted_ids", [])
    state.setdefault("state_version", STATE_VERSION)

    # v1 -> v2 migration: posted_ids is the permanent dedup source, and each
    # review entry needs a posted_at for the pruning window. Backfill both so an
    # older state object never re-posts or drops a review after this upgrade.
    if not isinstance(state["posted_ids"], list):
        raise RuntimeError(f"State 'posted_ids' must be a list: {where}")
    known = set(state["posted_ids"])
    for review_id in state["reviews"]:
        if review_id not in known:
            state["posted_ids"].append(review_id)
            known.add(review_id)
    fallback_posted_at = state.get("last_checked") or now_iso()
    for entry in state["reviews"].values():
        entry.setdefault("posted_at", fallback_posted_at)
    return state


# --------------------------------------------------------------------------- #
# Load / save (R2)
# --------------------------------------------------------------------------- #
def load_state(provider: str) -> dict:
    """Load provider state from R2. A missing object = fresh (initial sync)."""
    key = _state_key(provider)
    client = _client()
    try:
        response = client.get_object(Bucket=_bucket(), Key=key)
    except Exception as exc:
        if _is_missing(exc):
            LOG.info("No existing state at %s; starting fresh (initial sync)", key)
            _etags.pop(key, None)
            return _empty_state()
        raise
    _etags[key] = response.get("ETag")
    LOG.info("Loaded state from %s", key)
    return _parse_and_migrate(response["Body"].read().decode("utf-8"), key)


def _serialize(state: dict) -> bytes:
    state["state_version"] = STATE_VERSION
    state["last_checked"] = now_iso()
    state.setdefault("reviews", {})
    return (json.dumps(state, ensure_ascii=False, indent=4) + "\n").encode("utf-8")


def _save_with_retry(provider: str, state: dict, before_put=None) -> None:
    """Persist provider state to R2 with optimistic concurrency.

    On a conditional-put rejection (another run wrote the object since we read
    it), re-read, merge our changes onto the newer state, and retry — so a
    concurrent sync and reply never lose each other's progress.

    ``before_put`` (used by prune_and_save) runs at the top of every attempt,
    AFTER any merge and BEFORE the put, so pruning always operates on the latest
    merged state and is never undone by a concurrent writer (matching the old
    "merge then prune then commit" order).
    """
    from merge_state import merge_states

    key = _state_key(provider)
    client = _client()
    bucket = _bucket()

    for attempt in range(1, _MAX_SAVE_ATTEMPTS + 1):
        if before_put is not None:
            before_put(state)
        body = _serialize(state)
        etag = _etags.get(key)
        cond = {"IfMatch": etag} if etag else {"IfNoneMatch": "*"}
        try:
            response = client.put_object(
                Bucket=bucket,
                Key=key,
                Body=body,
                ContentType="application/json; charset=utf-8",
                CacheControl="no-cache",
                **cond,
            )
            _etags[key] = response.get("ETag")
            LOG.debug("Saved %s state to %s", provider, key)
            return
        except Exception as exc:
            if not _is_precondition_failed(exc):
                raise
            # Another writer got there first: re-read, merge, retry.
            try:
                fresh = client.get_object(Bucket=bucket, Key=key)
                remote = _parse_and_migrate(fresh["Body"].read().decode("utf-8"), key)
                _etags[key] = fresh.get("ETag")
            except Exception as get_exc:
                if _is_missing(get_exc):
                    # It was deleted between our put and read; create it next loop.
                    remote = _empty_state()
                    _etags.pop(key, None)
                else:
                    raise
            merged = merge_states(remote, state)
            state.clear()
            state.update(merged)
            LOG.info(
                "State %s changed under us; merged and retrying (attempt %d/%d)",
                key, attempt, _MAX_SAVE_ATTEMPTS,
            )

    raise RuntimeError(f"Could not persist state {key} after {_MAX_SAVE_ATTEMPTS} attempts")


def save_state(provider: str, state: dict) -> None:
    """Persist provider state to R2 (optimistic concurrency, no pruning)."""
    _save_with_retry(provider, state)


def _is_pristine(state: dict) -> bool:
    """A never-written state (e.g. an unconfigured provider) — don't create an
    empty R2 object for it."""
    return not (
        state.get("posted_ids")
        or state.get("reviews")
        or state.get("baselined")
        or state.get("last_review_id")
    )


def prune_and_save(provider: str, state: dict) -> None:
    """End-of-run write: prune inactive reviews, then persist — the merge+prune+
    commit the old git job did, now an R2 conditional write. No-op for a pristine
    provider so we never create an empty object for a store an app doesn't use.
    """
    if _is_pristine(state):
        return
    from prune_state import prune_inactive

    _save_with_retry(
        provider, state, before_put=lambda s: prune_inactive(s, datetime.now(timezone.utc))
    )


# --------------------------------------------------------------------------- #
# In-memory helpers (unchanged)
# --------------------------------------------------------------------------- #
def now_iso() -> str:
    """Current UTC time as an ISO-8601 string (used for posted_at/replied_at)."""
    return datetime.now(timezone.utc).isoformat()


def upsert_review(state: dict, review_id: str, **values) -> None:
    state.setdefault("reviews", {})
    state["reviews"].setdefault(review_id, {})
    state["reviews"][review_id].update(values)


def mark_posted(state: dict, review_id: str) -> None:
    """Record a review id at the TOP of the permanent dedup set (never pruned).

    posted_ids is kept newest-first: each new id is inserted at the front, so the
    oldest ids settle at the bottom of the list and can be trimmed by hand once
    they age out. Existing ids keep their relative order.
    """
    ids = state.setdefault("posted_ids", [])
    if review_id not in ids:
        ids.insert(0, review_id)
