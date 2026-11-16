"""One-time seed of the RAG corpus from developer replies already on the stores.

The fastest way to a useful corpus is the human replies your team has already
posted. Google Play returns them inline (``developerComment``); Apple exposes
them via the review's ``response`` relationship. This script fetches those,
writes one corpus pair per (review, existing reply), and exits. The next sync's
``rag.refresh_index`` embeds them.

Run it like a sync (same env: APP_CODE + the app's provider secrets + R2_*):

    APP_CODE=... R2_... APPSTORE_... GOOGLE_PLAY_... python scripts/backfill_corpus.py

It is idempotent — pairs are keyed by (platform, review_id), so re-running only
overwrites the same objects. A provider whose secrets are absent is skipped.
"""

import logging
import os

from common import corpus
from common.state_manager import now_iso


LOG = logging.getLogger(__name__)


def _store_pair(app_code: str, platform: str, review_id: str, review_text: str, reply_text: str, rating) -> bool:
    review_text = (review_text or "").strip()
    reply_text = (reply_text or "").strip()
    if not review_id or not review_text or not reply_text:
        return False
    corpus.write_pair(
        app_code,
        {
            "appcode": app_code,
            "platform": platform,
            "review_id": review_id,
            "rating": rating,
            "review_text": review_text,
            "reply_text": reply_text,
            "source": "store-backfill",
            "created_at": now_iso(),
        },
    )
    return True


def backfill_playstore(app_code: str) -> int:
    from providers.playstore import (
        REQUIRED_PLAYSTORE_ENV,
        _credentials,
        _rating_value,
        _user_comment,
        fetch_reviews,
    )

    if not all(os.environ.get(name) for name in REQUIRED_PLAYSTORE_ENV):
        LOG.info("Google Play not configured; skipping")
        return 0

    count = 0
    for review in fetch_reviews(_credentials()):
        comments = review.get("comments") if isinstance(review.get("comments"), list) else []
        dev_text = ""
        for comment in comments:
            dev = comment.get("developerComment") if isinstance(comment, dict) else None
            if isinstance(dev, dict) and (dev.get("text") or "").strip():
                dev_text = dev["text"].strip()
                break
        if not dev_text:
            continue
        comment = _user_comment(review)
        if _store_pair(
            app_code,
            "playstore",
            review.get("reviewId", ""),
            (comment.get("text") or "").strip(),
            dev_text,
            _rating_value(comment.get("starRating")),
        ):
            count += 1
    LOG.info("Backfilled %d Google Play pair(s)", count)
    return count


def backfill_appstore(app_code: str) -> int:
    from common.jwt_generator import generate_token
    from common.utils import request_with_retries
    from providers.appstore import APPLE_API, REQUIRED_APPSTORE_ENV, _apple_headers

    if not all(os.environ.get(name) for name in REQUIRED_APPSTORE_ENV):
        LOG.info("App Store not configured; skipping")
        return 0

    token = generate_token()
    # include=response folds each review's developer response into `included`,
    # so we read review text and its reply from one paged request.
    url = f"{APPLE_API}/apps/{os.environ['APPSTORE_APPLE_ID']}/customerReviews"
    params = {"limit": 200, "sort": "-createdDate", "include": "response"}
    count = 0
    pages = 0
    while url and pages < 25:
        response = request_with_retries(
            "GET", url, headers=_apple_headers(token), params=params, timeout=30,
            operation="Apple backfill customer reviews",
        )
        response.raise_for_status()
        data = response.json()
        responses = {
            item["id"]: (item.get("attributes") or {}).get("responseBody")
            for item in (data.get("included") or [])
            if isinstance(item, dict) and item.get("type") == "customerReviewResponses"
        }
        for review in data.get("data", []) or []:
            attr = review.get("attributes") or {}
            rel = ((review.get("relationships") or {}).get("response") or {}).get("data") or {}
            reply_text = responses.get(rel.get("id")) if isinstance(rel, dict) else None
            if not reply_text:
                continue
            title = (attr.get("title") or "").strip()
            body = (attr.get("body") or "").strip()
            review_text = f"{title}\n{body}" if title else body
            if _store_pair(app_code, "appstore", review.get("id", ""), review_text, reply_text, attr.get("rating", 0)):
                count += 1
        pages += 1
        url = (data.get("links") or {}).get("next")
        params = None
    LOG.info("Backfilled %d App Store pair(s)", count)
    return count


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    app_code = os.environ.get("APP_CODE", "").strip()
    if not app_code:
        LOG.error("APP_CODE is required")
        return 1

    total = 0
    for name, fn in (("App Store", backfill_appstore), ("Google Play", backfill_playstore)):
        try:
            total += fn(app_code)
        except Exception:
            LOG.warning("%s backfill failed; continuing", name, exc_info=True)
    LOG.info("Corpus backfill complete for %s: %d pair(s). Run a sync to embed them.", app_code, total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
