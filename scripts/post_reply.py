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

from common.publish import build_list, download_current, upload
from common.review_sync import reply_hash
from common.state_manager import load_state, now_iso, save_state


LOG = logging.getLogger(__name__)

REPLY_SENT_KEYS = {"appstore": "apple_reply_sent", "playstore": "google_reply_sent"}


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
        LOG.error("Review %s not active in %s state (pruned/expired?); skipping", review_id, platform)
        return "failed"

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
        }
    ]


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
    replier = _make_replier()
    sent = skipped = failed = 0
    for item in items:
        if not isinstance(item, dict):
            LOG.error("Reply item is not an object: %r", item)
            failed += 1
            continue
        try:
            result = _send_one(
                states,
                item.get("platform", ""),
                item.get("review_id", ""),
                item.get("reply_text", ""),
                replier,
            )
        except Exception:
            LOG.exception("Failed to send reply for review %r", item.get("review_id"))
            failed += 1
            continue
        sent += result == "sent"
        skipped += result == "skipped"
        failed += result == "failed"

    LOG.info("Replies processed: %d sent, %d skipped, %d failed", sent, skipped, failed)

    # Rebuild the pending-list ONCE so every replied review drops off the dashboard.
    app_details = {
        "appname": app_name,
        "appcode": app_code,
        "infisical_slug": os.environ.get("APP_INFISICAL_SLUG", "").strip(),
    }
    payload = build_list(download_current(app_code), states, [], app_details)
    upload(payload, app_code)

    # Success (so the commit step runs and persists state) when at least one
    # reply landed or was already sent; failure only when nothing succeeded.
    return 0 if (sent + skipped) > 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
