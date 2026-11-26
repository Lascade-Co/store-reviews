"""Send dashboard-approved replies to the store (workflow 2 entry point).

Inputs arrive as environment variables from the reply-review workflow. Two modes:
  - single (REPLY_ALL != "true" and no REPLIES): PLATFORM, REVIEW_ID, REPLY_TEXT
  - batch  (REPLY_ALL == "true" or REPLIES set): REPLIES = a JSON array of
        [{"platform": ..., "review_id": ..., "reply_text": ...}, ...]

Each reply is sent independently (one failure never blocks the rest); once all
are processed the app's pending-list file on R2 is rebuilt ONCE so the replied
reviews drop off the dashboard.
"""

import json
import logging
import os

from common import corpus
from common.publish import build_list, download_current, upload
from common.review_sync import reply_hash
from common.state_manager import load_state, now_iso, prune_and_save, save_state


LOG = logging.getLogger(__name__)

REPLY_SENT_KEYS = {"appstore": "apple_reply_sent", "playstore": "google_reply_sent"}


def _as_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"true", "1", "yes", "on"}


def _make_replier():
    """Return reply(platform, review_id, text); store auth is built once and reused
    across a batch (Apple JWT / Google token are created lazily on first use)."""
    cache: dict = {}

    def reply(platform: str, review_id: str, text: str) -> None:
        if platform == "appstore":
            if "appstore" not in cache:
                from common.jwt_generator import generate_token

                cache["appstore"] = generate_token()
            from providers.appstore import reply_to_review

            reply_to_review(cache["appstore"], review_id, text)
        else:
            if "playstore" not in cache:
                from providers.playstore import _credentials

                cache["playstore"] = _credentials()
            from providers.playstore import reply_to_review

            reply_to_review(cache["playstore"], review_id, text)

    return reply


def _send_one(states: dict, platform: str, review_id: str, reply_text: str, replier) -> str:
    """Send one reply and update state. Returns 'sent', 'skipped', or 'failed'."""
    platform = (platform or "").strip()
    review_id = (review_id or "").strip()
    reply_text = (reply_text or "").strip()
    if platform not in REPLY_SENT_KEYS:
        LOG.error("PLATFORM must be appstore or playstore, got %r", platform)
        return "failed"
    if not review_id or not reply_text:
        LOG.error("review_id and reply_text are both required (review %r)", review_id)
        return "failed"

    entry = states[platform].get("reviews", {}).get(review_id)
    if entry is None:
        # Benign: the review was already handled elsewhere and pruned, or expired.
        # It's a no-op (nothing to send), not a failure — don't drag the exit code.
        LOG.warning("Review %s not active in %s state (already handled/expired?); skipping", review_id, platform)
        return "skipped"

    text_hash = reply_hash(reply_text)
    if entry.get("last_sent_reply_hash") == text_hash:
        LOG.info("Identical reply already sent for %s; skipping (double-click protection)", review_id)
        return "skipped"

    LOG.info("Sending %s reply to review %s", platform, review_id)
    replier(platform, review_id, reply_text)

    entry["last_sent_reply_hash"] = text_hash
    entry["replied_at"] = now_iso()
    entry[REPLY_SENT_KEYS[platform]] = True
    save_state(platform, states[platform])
    return "sent"


def _resolve_items() -> list[dict] | None:
    """Build the list of replies from env. None signals a fatal input error."""
    replies_raw = os.environ.get("REPLIES", "").strip()
    reply_all = os.environ.get("REPLY_ALL", "").strip().lower() == "true"

    if reply_all or replies_raw:
        try:
            items = json.loads(replies_raw)
        except json.JSONDecodeError as exc:
            LOG.error("REPLIES is not valid JSON: %s", exc)
            return None
        if not isinstance(items, list) or not items:
            LOG.error("REPLIES must be a non-empty JSON array in batch mode")
            return None
        return items

    return [
        {
            "platform": os.environ.get("PLATFORM", ""),
            "review_id": os.environ.get("REVIEW_ID", ""),
            "reply_text": os.environ.get("REPLY_TEXT", ""),
            "is_human": os.environ.get("IS_HUMAN", ""),
        }
    ]


def _capture_corpus(item: dict, entry_index: dict, app_code: str) -> None:
    """On a human-written/edited send, drop a corpus pending file for this pair.

    Only human replies seed the corpus (never AI-accepted or auto-sent ones) —
    the echo-chamber rule. The review's words come from the dashboard entry
    (already on R2) so git state is never read for text. Best-effort: a failure
    logs and never fails the reply.
    """
    if not _as_bool(item.get("is_human")):
        return
    platform = (item.get("platform") or "").strip()
    review_id = str(item.get("review_id") or "").strip()
    reply_text = (item.get("reply_text") or "").strip()
    entry = entry_index.get((platform, review_id))
    if entry is None:
        LOG.info("No dashboard entry for %s %s; skipping corpus capture", platform, review_id)
        return
    key = f"{app_code}__{platform}__{review_id}"
    obj = {
        "key": key,
        "app": app_code,
        "type": os.environ.get("APP_TYPE", "").strip(),
        "store": platform,
        "review": (entry.get("body") or "").strip(),
        "reply": reply_text,
        "rating": entry.get("rating") or 0,
        "lang": entry.get("territory_or_language") or "",
        "is_human": True,
        "created_at": now_iso(),
    }
    for attempt in range(1, 4):
        try:
            corpus.write_pending(key, obj)
            LOG.info("Captured corpus pending file for %s", key)
            return
        except Exception:
            LOG.warning("Corpus capture failed for %s (attempt %d/3)", key, attempt, exc_info=True)
    LOG.warning("Giving up corpus capture for %s after 3 attempts", key)


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app_code = os.environ.get("APP_CODE", "").strip()
    app_name = os.environ.get("APP_NAME", "").strip() or app_code
    if not app_code:
        LOG.error("APP_CODE is required")
        return 1

    items = _resolve_items()
    if items is None:
        return 1

    states = {"appstore": load_state("appstore"), "playstore": load_state("playstore")}

    # Download the current dashboard file ONCE: it supplies the review words for
    # corpus capture below AND is reused to rebuild the pending-list at the end
    # (no second download).
    previous = download_current(app_code)
    entry_index: dict = {}
    if isinstance(previous, dict):
        for entry in previous.get("reviews", []):
            if isinstance(entry, dict):
                entry_index[(entry.get("platform"), str(entry.get("review_id")))] = entry

    replier = _make_replier()
    sent = skipped = 0
    failures: list[str] = []
    for item in items:
        if not isinstance(item, dict):
            LOG.error("Reply item is not an object: %r", item)
            failures.append("?")
            continue
        review_id = str(item.get("review_id") or "?")
        try:
            result = _send_one(
                states,
                item.get("platform", ""),
                review_id,
                item.get("reply_text", ""),
                replier,
            )
        except Exception:
            LOG.exception("Failed to send reply for review %s", review_id)
            failures.append(review_id)
            continue
        if result == "sent":
            sent += 1
            _capture_corpus(item, entry_index, app_code)
        elif result == "skipped":
            skipped += 1
        else:
            failures.append(review_id)

    failed = len(failures)
    LOG.info("Replies processed: %d sent, %d skipped, %d failed", sent, skipped, failed)
    # Surface failures as run annotations so a partial batch failure is visible in
    # the Actions UI even though the run stays green (green lets the commit step
    # persist the replies that DID land).
    for review_id in failures:
        print(f"::error title=Reply failed::Could not send reply for review {review_id}")

    # Rebuild the pending-list ONCE so every replied review drops off the dashboard.
    app_details = {
        "appname": app_name,
        "appcode": app_code,
        "infisical_slug": os.environ.get("APP_INFISICAL_SLUG", "").strip(),
    }
    payload = build_list(previous, states, [], app_details)
    upload(payload, app_code)

    # Persist each provider's state to R2 (prune + conditional write). Replaces
    # the old git commit step; a concurrent sync is reconciled by the merge.
    for provider, state in states.items():
        prune_and_save(provider, state)

    # Success when at least one reply landed or was already sent; failure only
    # when nothing succeeded.
    return 0 if (sent + skipped) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
