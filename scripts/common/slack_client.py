"""Minimal Slack client: the dashboard flow only posts run notifications."""

import logging
import os

from common.utils import request_with_retries


LOG = logging.getLogger(__name__)
SLACK_API = "https://slack.com/api"


class SlackApiError(RuntimeError):
    def __init__(self, method: str, error: str, retry_after: float | None = None):
        self.method = method
        self.error = error
        self.retry_after = retry_after
        super().__init__(f"Slack {method} failed: {error}")


class SlackPermissionError(SlackApiError):
    pass


class SlackClient:
    def __init__(self, token: str | None = None, channel_id: str | None = None):
        # Token resolution: an app may override the shared bot with its own
        # SLACK_BOT_TOKEN in its Infisical /reviews folder; when that key is
        # absent/empty, fall back to the shared default bot token that the
        # workflow provides from the central repo's GitHub secrets.
        self.token = (
            token
            or os.environ.get("SLACK_BOT_TOKEN", "").strip()
            or os.environ.get("SLACK_BOT_TOKEN_DEFAULT", "").strip()
        )
        if not self.token:
            raise RuntimeError("No Slack bot token: set SLACK_BOT_TOKEN or SLACK_BOT_TOKEN_DEFAULT")
        self.channel_id = channel_id or os.environ["SLACK_CHANNEL_ID"]

    def _call(self, method: str, payload: dict) -> dict:
        response = request_with_retries(
            "POST",
            f"{SLACK_API}/{method}",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            json=payload,
            timeout=30,
            # Notification posts are not retried on ambiguous failures to
            # avoid duplicate messages (429s are still retried upstream).
            retry_network_errors=False,
            retry_server_errors=False,
            operation=f"Slack {method}",
        )
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            try:
                retry_seconds = float(retry_after) if retry_after else None
            except ValueError:
                retry_seconds = None
            raise SlackApiError(method, "rate_limited", retry_seconds)
        response.raise_for_status()
        try:
            data = response.json()
        except ValueError as exc:
            raise SlackApiError(method, "invalid_json_response") from exc
        if not isinstance(data, dict):
            raise SlackApiError(method, "invalid_response_shape")
        if not data.get("ok"):
            error = str(data.get("error", "unknown_error"))
            if error in {"not_allowed_token_type", "missing_scope", "no_permission"}:
                raise SlackPermissionError(method, error)
            raise SlackApiError(method, error)
        return data

    def post_review(self, text: str) -> str:
        """Post one message to the app's channel; returns the message ts."""
        data = self._call("chat.postMessage", {"channel": self.channel_id, "text": text})
        ts = data.get("ts")
        if not ts:
            raise SlackApiError("chat.postMessage", "missing_ts")
        return ts
