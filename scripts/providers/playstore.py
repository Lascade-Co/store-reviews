"""Google Play review provider (web-dashboard mode).

Fetches customer reviews, records new ones in state, and returns normalized
entries for the app's pending-list data file. Replies are sent by the reply
workflow via reply_to_review().
"""

import json
import logging
import os
from datetime import datetime, timezone

from google.auth.transport.requests import Request
from google.oauth2 import service_account

from common.ai_reply import generate_suggested_replies
from common.review_sync import collect_new_reviews
from common.state_manager import load_state, now_iso, save_state
from common.utils import request_with_retries


LOG = logging.getLogger(__name__)
GOOGLE_PLAY_API = "https://androidpublisher.googleapis.com/androidpublisher/v3"
GOOGLE_PLAY_SCOPE = "https://www.googleapis.com/auth/androidpublisher"
INITIAL_SYNC_COUNT = 5
MAX_RESULTS = 100
MAX_REPLY_LENGTH = 350
PAGE_CAP = 10


def _package_name() -> str:
    package_name = os.environ.get("GOOGLE_PLAY_PACKAGE_NAME", "").strip()
    if not package_name:
        raise RuntimeError("GOOGLE_PLAY_PACKAGE_NAME is required")
    return package_name


def _credentials() -> service_account.Credentials:
    raw_json = os.environ.get("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON", "")
    if not raw_json:
        raise RuntimeError("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is required")
    try:
        info = json.loads(raw_json)
    except json.JSONDecodeError as exc:
        raise RuntimeError("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON is not valid JSON") from exc
    if not isinstance(info, dict):
        raise RuntimeError("GOOGLE_PLAY_SERVICE_ACCOUNT_JSON must contain a JSON object")
    try:
        credentials = service_account.Credentials.from_service_account_info(
            info, scopes=[GOOGLE_PLAY_SCOPE]
        )
        credentials.refresh(Request())
    except Exception as exc:
        raise RuntimeError("Could not authenticate with the Google Play service account") from exc
    if not credentials.token:
        raise RuntimeError("Google authentication returned no access token")
    return credentials


def _headers(credentials: service_account.Credentials) -> dict:
    return {
        "Authorization": f"Bearer {credentials.token}",
        "Content-Type": "application/json",
    }


def _timestamp_value(timestamp: object) -> float:
    if not isinstance(timestamp, dict):
        return 0.0
    try:
        seconds = float(timestamp.get("seconds", 0))
        nanos = float(timestamp.get("nanos", 0))
    except (TypeError, ValueError):
        return 0.0
    return seconds + nanos / 1_000_000_000


def _user_comments(review: dict) -> list[dict]:
    comments = review.get("comments")
    if not isinstance(comments, list):
        raise RuntimeError(f"Google Play review {review.get('reviewId')} has no comments list")
    return [
        comment["userComment"]
        for comment in comments
        if isinstance(comment, dict) and isinstance(comment.get("userComment"), dict)
    ]


def _user_comment(review: dict) -> dict:
    user_comments = _user_comments(review)
    if not user_comments:
        raise RuntimeError(f"Google Play review {review.get('reviewId')} has no user comment")
    return max(user_comments, key=lambda comment: _timestamp_value(comment.get("lastModified")))


def _has_developer_reply(review: dict) -> bool:
    comments = review.get("comments")
    if not isinstance(comments, list):
        return False
    return any(
        isinstance(comment, dict)
        and isinstance(comment.get("developerComment"), dict)
        and bool((comment["developerComment"].get("text") or "").strip())
        for comment in comments
    )


def _validate_review(review: object) -> None:
    if (
        not isinstance(review, dict)
        or not isinstance(review.get("reviewId"), str)
        or not review["reviewId"].strip()
    ):
        raise RuntimeError("Google Play review has an invalid resource shape")
    comment = _user_comment(review)
    rating = comment.get("starRating")
    if rating is not None and (isinstance(rating, bool) or not isinstance(rating, int) or not 1 <= rating <= 5):
        raise RuntimeError(f"Google Play review {review['reviewId']} has an invalid rating")
    if comment.get("text") is not None and not isinstance(comment.get("text"), str):
        raise RuntimeError(f"Google Play review {review['reviewId']} has invalid text")


def _log_failure(
    review_id: str,
    endpoint: str,
    message: str,
    status: object = "N/A",
    retry_attempt: str = "final_after_shared_retries",
) -> None:
    LOG.error(
        "provider=Google Play review_id=%s http_status=%s endpoint=%s "
        "retry_attempt=%s error=%s",
        review_id,
        status,
        endpoint,
        retry_attempt,
        message,
    )


def fetch_reviews(
    credentials: service_account.Credentials,
    max_pages: int = PAGE_CAP,
) -> list[dict]:
    """Fetch the whole Google Play review window, following pagination to the cap.

    Google returns roughly the last 7 days, so paging is naturally bounded; the
    cap is a safety net. There is deliberately no boundary early-stop: Google
    sorts by lastModified (mutable), so an edited boundary review can sort above
    a genuinely-new one — stopping at the boundary would drop that review.
    Dedup happens later against posted_ids.
    """
    package_name = _package_name()
    endpoint = f"{GOOGLE_PLAY_API}/applications/{package_name}/reviews"
    raw_reviews: list[dict] = []
    page_token = None
    pages = 0

    while pages < max_pages:
        params = {"maxResults": MAX_RESULTS}
        if page_token:
            params["token"] = page_token
        try:
            response = request_with_retries(
                "GET",
                endpoint,
                headers=_headers(credentials),
                params=params,
                timeout=30,
                operation="Google Play list reviews",
            )
        except Exception as exc:
            _log_failure("N/A", endpoint, str(exc))
            raise
        if not response.ok:
            _log_failure("N/A", endpoint, response.text[:500], response.status_code)
            response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            _log_failure("N/A", endpoint, "response was not valid JSON", response.status_code, "not_applicable")
            raise RuntimeError("Google Play reviews response was not valid JSON") from exc
        if not isinstance(data, dict) or not isinstance(data.get("reviews", []), list):
            _log_failure("N/A", endpoint, "response has no reviews list", response.status_code, "not_applicable")
            raise RuntimeError("Google Play reviews response has no reviews list")

        page = data.get("reviews", [])
        raw_reviews.extend(page)
        pages += 1

        pagination = data.get("tokenPagination")
        page_token = pagination.get("nextPageToken") if isinstance(pagination, dict) else None
        if not page_token:
            break

    if page_token:
        LOG.warning(
            "Google Play review pagination stopped at page cap %d; older reviews were not fetched this run",
            max_pages,
        )

    valid_reviews = []
    for review in raw_reviews:
        review_id = review.get("reviewId", "N/A") if isinstance(review, dict) else "N/A"
        try:
            _validate_review(review)
        except RuntimeError as exc:
            _log_failure(str(review_id), endpoint, str(exc), "N/A")
            continue
        valid_reviews.append(review)
    LOG.info("Fetched %d Google Play review(s) across %d page(s)", len(valid_reviews), pages)
    valid_reviews.sort(
        key=lambda review: _timestamp_value(_user_comment(review).get("lastModified")),
        reverse=True,
    )
    return valid_reviews


def _display_value(value: object, default: str) -> str:
    if value is None or not str(value).strip():
        return default
    return str(value).strip()


def _rating_value(value: object) -> int:
    if isinstance(value, int) and not isinstance(value, bool) and 1 <= value <= 5:
        return value
    return 0


def _prepare_reply(text: str, review_id: str) -> str:
    text = text.strip()
    if not text:
        raise ValueError("Cannot send an empty Google Play review response")
    if len(text) <= MAX_REPLY_LENGTH:
        return text
    truncated = text[:MAX_REPLY_LENGTH].rstrip()
    LOG.warning(
        "provider=Google Play review_id=%s reply_truncated=true original_length=%d final_length=%d limit=%d",
        review_id,
        len(text),
        len(truncated),
        MAX_REPLY_LENGTH,
    )
    return truncated


def reply_to_review(credentials: service_account.Credentials, review_id: str, text: str) -> None:
    text = _prepare_reply(text, review_id)

    package_name = _package_name()
    endpoint = f"{GOOGLE_PLAY_API}/applications/{package_name}/reviews/{review_id}:reply"
    try:
        response = request_with_retries(
            "POST",
            endpoint,
            headers=_headers(credentials),
            json={"replyText": text},
            timeout=30,
            retry_network_errors=False,
            retry_server_errors=False,
            operation=f"Google Play reply to review {review_id}",
        )
    except Exception as exc:
        _log_failure(review_id, endpoint, str(exc))
        raise
    if not response.ok:
        _log_failure(review_id, endpoint, response.text[:500], response.status_code)
        response.raise_for_status()
    try:
        data = response.json()
    except ValueError as exc:
        _log_failure(review_id, endpoint, "response was not valid JSON", response.status_code, "not_retried_for_write_safety")
        raise RuntimeError(f"Google Play reply response for {review_id} was not valid JSON") from exc
    # Google may normalize the applied reply (HTML-ish content stripped,
    # "approximately 350" chars), so result.replyText need not equal what was
    # sent. The reply was published either way — only a missing result/replyText
    # means failure; a mismatch is logged, never raised.
    result = data.get("result") if isinstance(data, dict) else None
    applied_text = result.get("replyText") if isinstance(result, dict) else None
    if not isinstance(applied_text, str) or not applied_text.strip():
        _log_failure(review_id, endpoint, "response did not contain an applied reply text", response.status_code, "not_applicable")
        raise RuntimeError(f"Google Play reply response for {review_id} was invalid")
    if applied_text != text:
        LOG.warning(
            "provider=Google Play review_id=%s reply_normalized=true: applied reply text differs from the sent text",
            review_id,
        )


def _review_id(review: dict) -> str:
    return review["reviewId"]


def _suggest_batch(new_reviews: list[dict]) -> dict[str, str]:
    """One Codex call for all new Google Play reviews → {review_id: reply}."""
    items = []
    for review in new_reviews:
        comment = _user_comment(review)
        items.append(
            {
                "id": review["reviewId"],
                "platform": "Google Play",
                "rating": _rating_value(comment.get("starRating")),
                "title": "",  # Google Play has no separate title field
                "body": _display_value(comment.get("text"), "No review text provided."),
            }
        )
    return generate_suggested_replies(items)


def normalize_entry(review: dict, suggested_reply: str | None) -> dict:
    """Convert a Google Play review into the dashboard data-file entry."""
    comment = _user_comment(review)
    text = _display_value(comment.get("text"), "No review text provided.")
    modified = _timestamp_value(comment.get("lastModified"))
    reviewed_at = (
        datetime.fromtimestamp(modified, tz=timezone.utc).isoformat() if modified else None
    )
    return {
        "platform": "playstore",
        "review_id": review["reviewId"],
        "rating": _rating_value(comment.get("starRating")),
        "title": None,  # Google Play has no separate title field
        "body": text.replace("\t", " — "),
        "reviewer": _display_value(review.get("authorName"), "Anonymous"),
        "territory_or_language": _display_value(comment.get("reviewerLanguage"), "Unknown"),
        "reviewed_at": reviewed_at,
        "detected_at": now_iso(),
        "suggested_reply": suggested_reply,
        "replied": False,
        "reply_text": None,
    }


REQUIRED_PLAYSTORE_ENV = (
    "GOOGLE_PLAY_PACKAGE_NAME",
    "GOOGLE_PLAY_SERVICE_ACCOUNT_JSON",
)


def run_playstore_collect() -> tuple[list[dict], dict]:
    """Fetch + record new reviews in state, return dashboard entries + state."""
    if not all(os.environ.get(name) for name in REQUIRED_PLAYSTORE_ENV):
        LOG.info("Google Play not configured for this app; skipping")
        return [], load_state("playstore")

    LOG.info("Generating Google Play OAuth access token")
    credentials = _credentials()
    state = load_state("playstore")
    initial_sync = not bool(state.get("last_review_id"))

    LOG.info("Fetching Google Play reviews%s", " (initial sync)" if initial_sync else "")
    reviews = fetch_reviews(credentials, max_pages=1) if initial_sync else fetch_reviews(credentials)
    LOG.info("Fetched %d Google Play review(s)", len(reviews))

    # Reviews already answered at the store (e.g. via the Play Console) carry a
    # developerComment; flag them so they never appear as pending.
    flags_changed = False
    for review in reviews:
        review_id = _review_id(review)
        entry = state.get("reviews", {}).get(review_id)
        if entry is not None and _has_developer_reply(review) and not entry.get("google_reply_sent"):
            entry["google_reply_sent"] = True
            flags_changed = True
    if flags_changed:
        save_state("playstore", state)

    entries = collect_new_reviews(
        "playstore",
        reviews,
        state,
        initial_sync,
        INITIAL_SYNC_COUNT,
        _review_id,
        normalize_entry,
        "google_reply_sent",
        reply_sent_getter=_has_developer_reply,
        # lastModified order is mutable; never stop at the boundary (see fetch_reviews).
        stop_at_boundary=False,
        baseline_all_fetched=True,
        suggestion_generator=_suggest_batch,
    ) if reviews else []
    return entries, state
