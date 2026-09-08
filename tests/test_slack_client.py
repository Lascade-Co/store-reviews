import unittest
from unittest.mock import patch

from common.slack_client import SlackApiError, SlackClient, SlackPermissionError


class Response:
    def __init__(self, status_code=200, data=None, headers=None):
        self.status_code = status_code
        self._data = data
        self.headers = headers or {}

    def json(self):
        return self._data

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class SlackTokenResolutionTests(unittest.TestCase):
    def test_infisical_token_wins_when_present(self):
        env = {"SLACK_BOT_TOKEN": "xoxb-per-app", "SLACK_BOT_TOKEN_DEFAULT": "xoxb-shared", "SLACK_CHANNEL_ID": "C1"}
        with patch.dict("os.environ", env, clear=False):
            self.assertEqual(SlackClient().token, "xoxb-per-app")

    def test_falls_back_to_default_when_absent_or_empty(self):
        for per_app in ("", "   "):
            env = {"SLACK_BOT_TOKEN": per_app, "SLACK_BOT_TOKEN_DEFAULT": "xoxb-shared", "SLACK_CHANNEL_ID": "C1"}
            with patch.dict("os.environ", env, clear=False):
                self.assertEqual(SlackClient().token, "xoxb-shared")

    def test_no_token_anywhere_raises(self):
        env = {"SLACK_BOT_TOKEN": "", "SLACK_BOT_TOKEN_DEFAULT": "", "SLACK_CHANNEL_ID": "C1"}
        with patch.dict("os.environ", env, clear=False):
            with self.assertRaises(RuntimeError):
                SlackClient()


class SlackClientTests(unittest.TestCase):
    def client(self) -> SlackClient:
        return SlackClient(token="test-token", channel_id="C123")

    def test_post_review_returns_message_ts(self):
        response = Response(data={"ok": True, "ts": "111.222"})
        with patch("common.slack_client.request_with_retries", return_value=response) as request:
            ts = self.client().post_review("hello")

        self.assertEqual(ts, "111.222")
        args, kwargs = request.call_args
        self.assertTrue(args[1].endswith("/chat.postMessage"))
        self.assertEqual(kwargs["json"], {"channel": "C123", "text": "hello"})

    def test_permission_error_is_actionable_type(self):
        response = Response(data={"ok": False, "error": "missing_scope"})
        with patch("common.slack_client.request_with_retries", return_value=response):
            with self.assertRaises(SlackPermissionError):
                self.client().post_review("hello")

    def test_rate_limit_preserves_retry_after(self):
        response = Response(status_code=429, headers={"Retry-After": "12"})
        with patch("common.slack_client.request_with_retries", return_value=response):
            with self.assertRaises(SlackApiError) as ctx:
                self.client().post_review("hello")

        self.assertEqual(ctx.exception.error, "rate_limited")
        self.assertEqual(ctx.exception.retry_after, 12.0)


if __name__ == "__main__":
    unittest.main()
