import json
import os
import unittest
from unittest.mock import Mock, patch

from common.ai_reply import (
    DEFAULT_GROQ_MODEL,
    GROQ_API_URL,
    MAX_SUGGESTED_REPLY_LENGTH,
    generate_suggested_reply,
)


def groq_response(content: str) -> Mock:
    response = Mock(ok=True, status_code=200, text="")
    response.json.return_value = {"choices": [{"message": {"content": content}}]}
    return response


class AiReplyTests(unittest.TestCase):
    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk-test"})
    @patch("common.ai_reply.request_with_retries")
    def test_generates_reply_from_json_content(self, request):
        request.return_value = groq_response(json.dumps({"reply": "Thanks for the feedback!"}))

        result = generate_suggested_reply("Google Play", 4, "Nice", "Works well")

        self.assertEqual(result, "Thanks for the feedback!")
        args, kwargs = request.call_args
        self.assertEqual(args, ("POST", GROQ_API_URL))
        self.assertEqual(kwargs["json"]["model"], DEFAULT_GROQ_MODEL)
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer gsk-test")
        self.assertEqual(kwargs["json"]["response_format"]["type"], "json_schema")
        self.assertIn("Works well", kwargs["json"]["messages"][1]["content"])

    @patch.dict(os.environ, {"GROQ_API_KEY": ""}, clear=False)
    @patch("common.ai_reply.request_with_retries")
    def test_missing_api_key_skips_without_request(self, request):
        self.assertIsNone(generate_suggested_reply("Google Play", 4, "T", "B"))
        request.assert_not_called()

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk-test"})
    @patch("common.ai_reply.request_with_retries")
    def test_http_error_returns_none(self, request):
        request.return_value = Mock(ok=False, status_code=429, text="rate limited")

        self.assertIsNone(generate_suggested_reply("Google Play", 1, "T", "B"))

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk-test"})
    @patch("common.ai_reply.request_with_retries")
    def test_network_exception_returns_none(self, request):
        request.side_effect = RuntimeError("connection reset")

        self.assertIsNone(generate_suggested_reply("Apple App Store", 5, "T", "B"))

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk-test"})
    @patch("common.ai_reply.request_with_retries")
    def test_plain_text_content_is_accepted_as_fallback(self, request):
        request.return_value = groq_response("Sorry to hear that — we're on it.")

        result = generate_suggested_reply("Apple App Store", 2, "T", "B")

        self.assertEqual(result, "Sorry to hear that — we're on it.")

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk-test"})
    @patch("common.ai_reply.request_with_retries")
    def test_overlong_reply_is_clamped(self, request):
        request.return_value = groq_response(json.dumps({"reply": "x" * 600}))

        result = generate_suggested_reply("Google Play", 3, "T", "B")

        self.assertEqual(len(result), MAX_SUGGESTED_REPLY_LENGTH)

    @patch.dict(os.environ, {"GROQ_API_KEY": "gsk-test"})
    @patch("common.ai_reply.request_with_retries")
    def test_empty_or_malformed_response_returns_none(self, request):
        for body in (
            {},
            {"choices": []},
            {"choices": [{"message": {"content": ""}}]},
            {"choices": [{"message": {"content": json.dumps({"reply": "   "})}}]},
        ):
            response = Mock(ok=True, status_code=200, text="")
            response.json.return_value = body
            request.return_value = response

            self.assertIsNone(generate_suggested_reply("Google Play", 3, "T", "B"))


if __name__ == "__main__":
    unittest.main()
