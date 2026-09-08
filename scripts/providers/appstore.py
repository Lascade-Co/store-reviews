"""Apple App Store review provider (web-dashboard mode).

Fetches customer reviews, records new ones in state, and returns normalized
entries for the app's pending-list data file. Replies are sent by the reply
workflow via reply_to_review().
"""

import logging
import os

from common.ai_reply import generate_suggested_reply
from common.jwt_generator import generate_token
from common.review_sync import collect_new_reviews
from common.state_manager import load_state, now_iso
from common.utils import request_with_retries


LOG = logging.getLogger(__name__)
APPLE_API = "https://api.appstoreconnect.apple.com/v1"
INITIAL_SYNC_COUNT = 5
PAGE_CAP = 25


def _apple_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _validate_review(review: object) -> None:
    if not isinstance(review, dict) or not isinstance(review.get("id"), str):
        raise RuntimeError("Apple review has an invalid resource shape")
    attributes = review.get("attributes")
    if not isinstance(attributes, dict) or not isinstance(attributes.get("createdDate"), str):
        raise RuntimeError(f"Apple review {review['id']} has invalid attributes")
    rating = attributes.get("rating", 0)
    if not isinstance(rating, int) or not 0 <= rating <= 5:
        raise RuntimeError(f"Apple review {review['id']} has an invalid rating")


def fetch_reviews(token: str, stop_at_id: str | None = None, max_pages: int = PAGE_CAP) -> list[dict]:
    """Fetch Apple reviews, following pagination until the boundary or the cap.

    Apple sorts by createdDate (immutable), so once ``stop_at_id`` appears in a
    page everything older is already known and paging can stop.
    """
    url = f"{APPLE_API}/apps/{os.environ['APPSTORE_APPLE_ID']}/customerReviews"
    params = {"limit": 200, "sort": "-createdDate"}
    reviews: list[dict] = []
    pages = 0
    stopped_early = False

    while url and pages < max_pages:
        response = request_with_retries(
            "GET",
            url,
            headers=_apple_headers(token),
            params=params,
            timeout=30,
            operation="Apple list customer reviews",
        )
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("Apple customer reviews response was not valid JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("data"), list):
            raise RuntimeError("Apple customer reviews response has no data list")

        page = data["data"]
        for review in page:
            _validate_review(review)
        reviews.extend(page)
        pages += 1

        if stop_at_id is not None and any(review["id"] == stop_at_id for review in page):
            stopped_early = True
            break

        # Apple's next link already carries the cursor + query; drop params.
        url = (data.get("links") or {}).get("next")
        params = None

    if url and not stopped_early:
        LOG.warning(
            "Apple review pagination stopped at page cap %d; older reviews were not fetched this run",
            max_pages,
        )
    LOG.info("Fetched %d Apple review(s) across %d page(s)", len(reviews), pages)
    reviews.sort(key=lambda review: review["attributes"]["createdDate"], reverse=True)
    return reviews


def reply_to_review(token: str, review_id: str, text: str) -> None:
    """Create or replace Apple's single response for a customer review."""
    text = text.strip()
    if not text:
        raise ValueError("Cannot send an empty Apple review response")

    payload = {
        "data": {
            "type": "customerReviewResponses",
            "attributes": {"responseBody": text},
            "relationships": {
                "review": {"data": {"type": "customerReviews", "id": review_id}}
            },
        }
    }
    response = request_with_retries(
        "POST",
        f"{APPLE_API}/customerReviewResponses",
        headers=_apple_headers(token),
        json=payload,
        timeout=30,
        retry_network_errors=False,
        retry_server_errors=False,
        operation=f"Apple reply to review {review_id}",
    )
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Apple reply response for {review_id} was not valid JSON") from exc
    resource = data.get("data") if isinstance(data, dict) else None
    if not isinstance(resource, dict) or resource.get("type") != "customerReviewResponses":
        raise RuntimeError(f"Apple reply response for {review_id} was invalid")


def _review_id(review: dict) -> str:
    return review["id"]


def _suggest_reply(review: dict) -> str | None:
    """Ask the AI for a suggested response to this review (None on any failure)."""
    attr = review.get("attributes", {})
    return generate_suggested_reply(
        "Apple App Store",
        attr.get("rating", 0),
        str(attr.get("title") or "").strip() or "No Title",
        str(attr.get("body") or "").strip() or "No review text provided.",
    )


def normalize_entry(review: dict, suggested_reply: str | None) -> dict:
    """Convert an Apple review into the dashboard data-file entry."""
    attr = review.get("attributes", {})
    return {
        "platform": "appstore",
        "review_id": review["id"],
        "rating": attr.get("rating", 0),
        "title": str(attr.get("title") or "").strip() or None,
        "body": str(attr.get("body") or "").strip() or "No review text provided.",
        "reviewer": str(attr.get("reviewerNickname") or "Anonymous"),
        "territory_or_language": str(attr.get("territory") or "Unknown"),
        "reviewed_at": attr.get("createdDate"),
        "detected_at": now_iso(),
        "suggested_reply": suggested_reply,
        "replied": False,
        "reply_text": None,
    }


REQUIRED_APPSTORE_ENV = (
    "APPSTORE_API_KEY_ID",
    "APPSTORE_ISSUER_ID",
    "APPSTORE_API_PRIVATE_KEY",
    "APPSTORE_APPLE_ID",
)


def run_appstore_collect() -> tuple[list[dict], dict]:
    """Fetch + record new reviews in state, return dashboard entries + state."""
    if not all(os.environ.get(name) for name in REQUIRED_APPSTORE_ENV):
        LOG.info("App Store not configured for this app; skipping")
        return [], load_state("appstore")

    LOG.info("Generating App Store Connect JWT")
    token = generate_token()
    state = load_state("appstore")
    initial_sync = not bool(state.get("last_review_id"))

    LOG.info("Fetching App Store reviews%s", " (initial sync)" if initial_sync else "")
    if initial_sync:
        # Only the newest page is needed to publish the first few reviews.
        reviews = fetch_reviews(token, max_pages=1)
    else:
        reviews = fetch_reviews(token, stop_at_id=state.get("last_review_id"))
    LOG.info("Fetched %d review(s)", len(reviews))

    entries = collect_new_reviews(
        "appstore",
        reviews,
        state,
        initial_sync,
        INITIAL_SYNC_COUNT,
        _review_id,
        normalize_entry,
        "apple_reply_sent",
        # createdDate order is immutable, so stopping the scan at the
        # last_review_id boundary is safe for Apple (unlike Google).
        stop_at_boundary=True,
        suggestion_generator=_suggest_reply,
    ) if reviews else []
    return entries, state
