"""Send one dashboard-approved reply to the store (workflow 2 entry point).

Inputs arrive as environment variables from the reply-review workflow's
dispatch inputs: PROJECT_SLUG, PLATFORM (appstore|playstore), REVIEW_ID,
REPLY_TEXT. After the store accepts the reply, state is updated and the app's
pending-list file is rebuilt (the replied entry drops out) and re-uploaded.
"""

import logging
import os
import sys

from common.publish import build_list, download_current, upload
from common.review_sync import reply_hash
from common.state_manager import load_state, now_iso, save_state


LOG = logging.getLogger(__name__)

REPLY_SENT_KEYS = {"appstore": "apple_reply_sent", "playstore": "google_reply_sent"}


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    slug = os.environ.get("PROJECT_SLUG", "").strip()
    platform = os.environ.get("PLATFORM", "").strip()
    review_id = os.environ.get("REVIEW_ID", "").strip()
    reply_text = os.environ.get("REPLY_TEXT", "").strip()

    if platform not in REPLY_SENT_KEYS:
        LOG.error("PLATFORM must be appstore or playstore, got %r", platform)
        return 1
    if not slug or not review_id or not reply_text:
        LOG.error("PROJECT_SLUG, REVIEW_ID, and REPLY_TEXT are all required")
        return 1

    state = load_state(platform)
    entry = state.get("reviews", {}).get(review_id)
    if entry is None:
        LOG.error(
            "Review %s is not active in %s state (already pruned/expired?); nothing sent",
            review_id,
            platform,
        )
        return 1

    text_hash = reply_hash(reply_text)
    if entry.get("last_sent_reply_hash") == text_hash:
        LOG.info("Identical reply already sent for %s; skipping (double-click protection)", review_id)
        return 0

    LOG.info("Sending dashboard reply to %s review %s", platform, review_id)
    if platform == "appstore":
        from common.jwt_generator import generate_token
        from providers.appstore import reply_to_review

        reply_to_review(generate_token(), review_id, reply_text)
    else:
        from providers.playstore import _credentials, reply_to_review

        reply_to_review(_credentials(), review_id, reply_text)

    entry["last_sent_reply_hash"] = text_hash
    entry["replied_at"] = now_iso()
    entry[REPLY_SENT_KEYS[platform]] = True
    save_state(platform, state)
    LOG.info("Reply accepted by the store; state updated")

    # Rebuild the pending-list so the dashboard stops showing this review.
    states = {"appstore": load_state("appstore"), "playstore": load_state("playstore")}
    payload = build_list(download_current(slug), states, [], slug)
    upload(payload, slug)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
