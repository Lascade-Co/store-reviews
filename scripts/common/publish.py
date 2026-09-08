"""Build and publish the per-app pending-reviews file for the web dashboard.

The data file is a living pending-list: each run appends new reviews (full
content captured at fetch time) and drops replied/expired entries. State stays
the authority on which entries exist; the file carries the words between runs
(git state deliberately stores no review content). The payload is stored as
plain JSON — access control is the unguessable R2 path prefix, and plain JSON
keeps the file directly inspectable when debugging.
"""

import json
import logging
import os

from common.slack_client import SlackClient
from common.state_manager import now_iso


LOG = logging.getLogger(__name__)


def _require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} is required to publish the dashboard data file")
    return value


def _r2_client():
    # R2 speaks the S3 API; boto3 is the supported client. Imported lazily so
    # the test suite and Slack-mode runs never need boto3 installed.
    import boto3
    from botocore.config import Config

    account_id = _require_env("R2_ACCOUNT_ID")
    return boto3.client(
        "s3",
        endpoint_url=f"https://{account_id}.r2.cloudflarestorage.com",
        aws_access_key_id=_require_env("R2_ACCESS_KEY_ID"),
        aws_secret_access_key=_require_env("R2_SECRET_ACCESS_KEY"),
        region_name="auto",
        # Newer boto3 defaults to streaming CRC checksums that R2's S3 layer
        # does not accept on all operations; compute only when required.
        config=Config(
            request_checksum_calculation="when_required",
            response_checksum_validation="when_required",
        ),
    )


def _object_key(slug: str) -> str:
    prefix = _require_env("R2_DATA_PREFIX").strip("/")
    return f"{prefix}/{slug}.json"


def encode_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def decode_payload(blob: str) -> dict | None:
    try:
        data = json.loads(blob)
    except Exception:
        LOG.warning("Existing dashboard data file could not be parsed; starting a fresh list")
        return None
    return data if isinstance(data, dict) else None


def download_current(slug: str) -> dict | None:
    """Fetch the previous pending-list from R2, or None when absent/invalid."""
    client = _r2_client()
    try:
        response = client.get_object(Bucket=_require_env("R2_BUCKET"), Key=_object_key(slug))
    except client.exceptions.NoSuchKey:
        return None
    except Exception as exc:  # first run, missing bucket perms, etc.
        LOG.warning("Could not download previous dashboard file: %s", exc)
        return None
    return decode_payload(response["Body"].read().decode("utf-8"))


def upload(payload: dict, slug: str) -> None:
    client = _r2_client()
    client.put_object(
        Bucket=_require_env("R2_BUCKET"),
        Key=_object_key(slug),
        Body=encode_payload(payload).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
        # The page must always see the latest upload through the fixed URL.
        CacheControl="no-cache",
    )
    LOG.info("Uploaded dashboard data file for %s (%d pending review(s))", slug, len(payload.get("reviews", [])))


def _entry_is_pending(entry: dict, states: dict) -> bool:
    """Keep an entry only while its review is active in state and un-replied."""
    state = states.get(entry.get("platform"))
    if not isinstance(state, dict):
        return False
    review = state.get("reviews", {}).get(entry.get("review_id"))
    if not isinstance(review, dict):
        return False  # pruned/expired
    if review.get("last_sent_reply_hash"):
        return False  # replied via the dashboard — never shown again
    if any(key.endswith("_reply_sent") and value for key, value in review.items()):
        return False  # already answered at the store (e.g. via the console)
    return True


def build_list(previous: dict | None, states: dict, new_entries: list[dict], slug: str) -> dict:
    """previous non-replied entries + new reviews; state decides both."""
    reviews: list[dict] = []
    seen: set[tuple] = set()
    old_reviews = previous.get("reviews", []) if isinstance(previous, dict) else []
    for entry in old_reviews:
        if not isinstance(entry, dict):
            continue
        key = (entry.get("platform"), entry.get("review_id"))
        if key in seen or not _entry_is_pending(entry, states):
            continue
        seen.add(key)
        reviews.append(entry)
    for entry in new_entries:
        key = (entry.get("platform"), entry.get("review_id"))
        if key in seen or not _entry_is_pending(entry, states):
            continue
        seen.add(key)
        reviews.append(entry)
    reviews.sort(key=lambda item: item.get("reviewed_at") or "", reverse=True)
    return {"project_slug": slug, "generated_at": now_iso(), "reviews": reviews}


def notify_slack(new_count: int, slug: str) -> None:
    """One message per run, only when new reviews arrived."""
    if new_count <= 0:
        return
    site = os.environ.get("SITE_BASE_URL", "").strip().rstrip("/")
    if not site:
        LOG.warning("SITE_BASE_URL not set; skipping Slack notification")
        return
    plural = "s" if new_count != 1 else ""
    try:
        slack = SlackClient()
        slack.post_review(
            f"🆕 {new_count} new review{plural} for *{slug}* — "
            f"review and reply at {site}/?app={slug}"
        )
    except Exception:
        # Notification is best-effort: the data file is already uploaded.
        LOG.warning("Slack notification failed; dashboard data was published anyway", exc_info=True)


def publish(slug: str, states: dict, new_entries: list[dict]) -> None:
    """Merge, upload, and notify. states = {"appstore": ..., "playstore": ...}."""
    previous = download_current(slug)
    payload = build_list(previous, states, new_entries, slug)
    upload(payload, slug)
    notify_slack(len(new_entries), slug)
