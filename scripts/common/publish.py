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


def _object_key(app_code: str) -> str:
    prefix = _require_env("R2_DATA_PREFIX").strip("/")
    return f"{prefix}/{app_code}.json"


def encode_payload(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def decode_payload(blob: str) -> dict | None:
    try:
        data = json.loads(blob)
    except Exception:
        LOG.warning("Existing dashboard data file could not be parsed; starting a fresh list")
        return None
    return data if isinstance(data, dict) else None


def download_current(app_code: str) -> dict | None:
    """Fetch the previous pending-list from R2, or None when absent/invalid."""
    client = _r2_client()
    try:
        response = client.get_object(Bucket=_require_env("R2_BUCKET"), Key=_object_key(app_code))
    except client.exceptions.NoSuchKey:
        return None
    except Exception as exc:  # first run, missing bucket perms, etc.
        LOG.warning("Could not download previous dashboard file: %s", exc)
        return None
    return decode_payload(response["Body"].read().decode("utf-8"))


def upload(payload: dict, app_code: str) -> None:
    client = _r2_client()
    client.put_object(
        Bucket=_require_env("R2_BUCKET"),
        Key=_object_key(app_code),
        Body=encode_payload(payload).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
        # The page must always see the latest upload through the fixed URL.
        CacheControl="no-cache",
    )
    LOG.info("Uploaded dashboard data file for %s (%d pending review(s))", app_code, len(payload.get("reviews", [])))


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


def build_list(
    previous: dict | None,
    states: dict,
    new_entries: list[dict],
    app_details: dict,
) -> dict:
    """previous non-replied entries + new reviews; state decides both.

    ``app_details`` is the app object from apps.json ({appname, appcode,
    infisical_slug}); it is stored under "app_details" so the dashboard reads the
    display name and code straight from the data file (keyed only by appcode in
    the URL).
    """
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
    return {
        "app_details": app_details,
        "generated_at": now_iso(),
        "reviews": reviews,
    }


def notify_slack(new_count: int, app_details: dict) -> None:
    """One message per run, only when new reviews arrived."""
    if new_count <= 0:
        return
    site = os.environ.get("SITE_BASE_URL", "").strip().rstrip("/")
    if not site:
        LOG.warning("SITE_BASE_URL not set; skipping Slack notification")
        return
    app_name = app_details.get("appname") or app_details.get("appcode", "")
    app_code = app_details.get("appcode", "")
    try:
        slack = SlackClient()
        # Labelled fields (bold labels via *…*). Slack trims real leading/trailing
        # whitespace, so U+2800 (Braille blank — a printable Symbol, not
        # whitespace) is used to frame the message with a blank line top and bottom.
        slack.post_review(
            f"*App Code:* {app_code}\n"
            f"*App Name:* {app_name}\n"
            f"*New Reviews:* {new_count}\n"
            f"*Review URL:* {site}/?app={app_code}\n"
            f"⠀\n"

        )
    except Exception:
        # Notification is best-effort: the data file is already uploaded.
        LOG.warning("Slack notification failed; dashboard data was published anyway", exc_info=True)


def publish(app_details: dict, states: dict, new_entries: list[dict]) -> None:
    """Merge, upload, and notify. states = {"appstore": ..., "playstore": ...}.

    ``app_details`` is the app object from apps.json ({appname, appcode,
    infisical_slug}); appcode names the R2 file and dashboard link.
    """
    app_code = app_details["appcode"]
    previous = download_current(app_code)
    payload = build_list(previous, states, new_entries, app_details)
    upload(payload, app_code)
    # Count only NEW reviews that are actually pending — i.e. shown on the
    # dashboard. A freshly-fetched review that already had a store reply is
    # recorded but filtered out of the dashboard; it must not inflate the
    # "new reviews received" number so Slack matches what the dashboard shows.
    new_pending = sum(1 for entry in new_entries if _entry_is_pending(entry, states))
    notify_slack(new_pending, app_details)
