"""Apple App Store review synchronization provider.

Same flow as the Google Play provider (playstore.py) and the same shared
helpers: fetch_reviews -> post_new_reviews -> sync_slack_replies. Only the
provider-specific parts differ (JWT auth, JSON:API pagination, and the
immutable createdDate ordering that allows a boundary early-stop).
"""

import copy
import logging
import os

from common.jwt_generator import generate_token
from common.review_sync import post_new_reviews, sync_slack_replies
from common.slack_client import SlackClient
from common.state_manager import load_state, save_if_changed
from common.utils import current_ist, request_with_retries, utc_to_ist


LOG = logging.getLogger(__name__)
APPLE_API = "https://api.appstoreconnect.apple.com/v1"
INITIAL_SYNC_COUNT = 5
PAGE_CAP = 25


def _apple_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _escape_slack(value: object) -> str:
    return str(value or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def format_review(review: dict) -> str:
    """Format an Apple review without allowing review content to alter Slack markup."""
    attr = review["attributes"]
    rating = attr.get("rating", 0)
    title = _escape_slack(str(attr.get("title") or "").strip() or "No Title")
    body = _escape_slack(str(attr.get("body") or "").strip() or "No review text provided.")
    reviewer = _escape_slack(attr.get("reviewerNickname", "Anonymous"))
    territory = _escape_slack(attr.get("territory", "Unknown"))
    reviewed_on = utc_to_ist(attr["createdDate"])
    review_id = _escape_slack(review["id"])

    return f"""
*New Appstore Review*

*Rating:* {rating}/5
*Title:* {title}
*Review:* {body}

*Reviewer:* {reviewer}
*Country:* {territory}
*Reviewed:* {reviewed_on}
*Platform:* Apple App Store
*Review ID:* {review_id}
*Detected:* {current_ist()}

"""


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
    url = f"{APPLE_API}/apps/{os.environ['APPSTORE_APP_ID']}/customerReviews"
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


REQUIRED_APPSTORE_ENV = (
    "APPSTORE_API_KEY_ID",
    "APPSTORE_ISSUER_ID",
    "APPSTORE_API_PRIVATE_KEY",
    "APPSTORE_APP_ID",
)


def run_appstore() -> None:
    """Run one App Store sync: fetch reviews, post new ones, apply Slack replies."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    # Provider guard: an Android-only app has no APPSTORE_* keys in its
    # Infisical /reviews folder, so this job exits successfully without work.
    if not all(os.environ.get(name) for name in REQUIRED_APPSTORE_ENV):
        LOG.info("App Store not configured for this app; skipping")
        return

    LOG.info("Generating App Store Connect JWT")
    token = generate_token()
    state = load_state("appstore")
    original_state = copy.deepcopy(state)
    # No last_review_id recorded yet means this app has never synced before.
    initial_sync = not bool(state.get("last_review_id"))
    slack = SlackClient()

    def post_to_slack(reviews: list[dict]) -> None:
        # Shared posting logic (same call the Google Play provider makes):
        # dedups against posted_ids, posts oldest-first, saves each Slack
        # thread mapping, then advances last_review_id.
        post_new_reviews(
            "appstore",
            reviews,
            state,
            slack,
            initial_sync,
            INITIAL_SYNC_COUNT,
            _review_id,
            format_review,
            "apple_reply_sent",
            # createdDate order is immutable, so stopping the scan at the
            # last_review_id boundary is safe for Apple (unlike Google).
            stop_at_boundary=True,
        )

    # Fetch: the first run needs only one page (it posts just the newest few);
    # incremental runs page until the last-seen review (createdDate order is
    # immutable, so stopping at the boundary is safe for Apple).
    LOG.info("Fetching App Store reviews%s", " (initial sync)" if initial_sync else "")
    if initial_sync:
        reviews = fetch_reviews(token, max_pages=1)
    else:
        reviews = fetch_reviews(token, stop_at_id=state.get("last_review_id"))
    LOG.info("Fetched %d review(s)", len(reviews))
    if reviews:
        post_to_slack(reviews)

    if initial_sync:
        # Replies are NOT polled on the first run, so pre-existing Slack
        # messages can never be mistaken for store replies.
        if reviews:
            LOG.info("Initial sync complete; saved %d review mapping(s)", len(state.get("reviews", {})))
        else:
            LOG.info("Initial sync found no reviews")
        return

    # Incremental run continues: poll the active Slack threads and forward
    # the newest human reply to the store.
    sync_slack_replies(
        "appstore",
        state,
        slack,
        "apple_reply_sent",
        lambda review_id, text: reply_to_review(token, review_id, text),
        "Apple App Store",
    )

    if state != original_state:
        save_if_changed("appstore", original_state, state)
        LOG.info("State updated")
    else:
        LOG.info("No state changes")
