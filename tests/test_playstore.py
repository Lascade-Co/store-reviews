import unittest
from unittest.mock import Mock, patch

from providers.playstore import (
    _has_developer_reply,
    _prepare_reply,
    _review_id,
    _timestamp_value,
    fetch_reviews,
    normalize_entry,
    reply_to_review,
)


def review(review_id="play-1"):
    return {
        "reviewId": review_id,
        "authorName": "A reviewer",
        "comments": [
            {
                "userComment": {
                    "text": "Excellent app",
                    "starRating": 5,
                    "reviewerLanguage": "en-IN",
                    "appVersionName": "2.3.1",
                    "lastModified": {"seconds": "1700000000", "nanos": 0},
                }
            }
        ],
    }


class PlayStoreTests(unittest.TestCase):
    def test_timestamp_shape_is_supported(self):
        self.assertEqual(_timestamp_value({"seconds": "10", "nanos": 500000000}), 10.5)

    def test_review_id_is_taken_directly_from_api_resource(self):
        self.assertEqual(_review_id(review("actual-api-id")), "actual-api-id")

    def test_developer_comment_is_detected(self):
        review_with_reply = review()
        review_with_reply["comments"].append(
            {"developerComment": {"text": "Already answered in the console"}}
        )
        self.assertTrue(_has_developer_reply(review_with_reply))
        self.assertFalse(_has_developer_reply(review()))

    def test_normalize_entry_shapes_dashboard_fields(self):
        entry = normalize_entry(review("g1"), "¡Gracias!")

        self.assertEqual(entry["platform"], "playstore")
        self.assertEqual(entry["review_id"], "g1")
        self.assertIsNone(entry["title"])  # Google Play has no separate title
        self.assertEqual(entry["body"], "Excellent app")
        self.assertEqual(entry["reviewer"], "A reviewer")
        self.assertEqual(entry["suggested_reply"], "¡Gracias!")
        self.assertTrue(entry["reviewed_at"].startswith("2023-11-14"))

    def test_normalize_entry_handles_missing_optional_fields(self):
        empty_review = {
            "reviewId": "play-empty",
            "comments": [{"userComment": {"text": None, "starRating": None}}],
        }
        entry = normalize_entry(empty_review, None)

        self.assertEqual(entry["body"], "No review text provided.")
        self.assertEqual(entry["rating"], 0)
        self.assertEqual(entry["reviewer"], "Anonymous")
        self.assertIsNone(entry["suggested_reply"])

    def test_reply_is_truncated_to_documented_limit(self):
        prepared = _prepare_reply("x" * 400, "play-1")

        self.assertEqual(len(prepared), 350)
        self.assertEqual(prepared, "x" * 350)

    @patch("providers.playstore._package_name", return_value="com.example.app")
    @patch("providers.playstore.request_with_retries")
    def test_invalid_review_schema_is_skipped(self, request, package_name):
        response = Mock(ok=True, status_code=200, text="")
        response.json.return_value = {
            "reviews": [
                {"reviewId": "bad", "comments": [{"userComment": {"starRating": 6}}]},
                review("valid"),
            ]
        }
        request.return_value = response

        result = fetch_reviews(Mock(token="access-token"))

        self.assertEqual([item["reviewId"] for item in result], ["valid"])

    @patch("providers.playstore._package_name", return_value="com.example.app")
    @patch("providers.playstore.request_with_retries")
    def test_fetch_reviews_follows_next_page_token(self, request, package_name):
        page1 = Mock(ok=True, status_code=200, text="")
        page1.json.return_value = {
            "reviews": [review("r1")],
            "tokenPagination": {"nextPageToken": "T2"},
        }
        page2 = Mock(ok=True, status_code=200, text="")
        page2.json.return_value = {"reviews": [review("r2")]}
        request.side_effect = [page1, page2]

        result = fetch_reviews(Mock(token="access-token"))

        self.assertEqual(request.call_count, 2)
        self.assertEqual({item["reviewId"] for item in result}, {"r1", "r2"})

    @patch("providers.playstore._package_name", return_value="com.example.app")
    @patch("providers.playstore.request_with_retries")
    def test_fetch_reviews_does_not_stop_at_a_known_review(self, request, package_name):
        # Google's lastModified order is mutable: an edited boundary review can
        # sort above a genuinely-new one, so fetch must read the whole window.
        page1 = Mock(ok=True, status_code=200, text="")
        page1.json.return_value = {
            "reviews": [review("boundary")],
            "tokenPagination": {"nextPageToken": "T2"},
        }
        page2 = Mock(ok=True, status_code=200, text="")
        page2.json.return_value = {"reviews": [review("new")]}
        request.side_effect = [page1, page2]

        result = fetch_reviews(Mock(token="access-token"))

        self.assertEqual(request.call_count, 2)
        self.assertEqual({item["reviewId"] for item in result}, {"boundary", "new"})

    @patch("providers.playstore._package_name", return_value="com.example.app")
    @patch("providers.playstore.request_with_retries")
    def test_reply_uses_official_endpoint_and_payload(self, request, package_name):
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"result": {"replyText": "Thanks"}}
        request.return_value = response
        credentials = Mock(token="access-token")

        reply_to_review(credentials, "play-1", "Thanks")

        request.assert_called_once()
        args, kwargs = request.call_args
        self.assertEqual(args[0], "POST")
        self.assertTrue(args[1].endswith("/applications/com.example.app/reviews/play-1:reply"))
        self.assertEqual(kwargs["json"], {"replyText": "Thanks"})

    @patch("providers.playstore._package_name", return_value="com.example.app")
    @patch("providers.playstore.request_with_retries")
    def test_normalized_reply_text_is_accepted(self, request, package_name):
        # Google may strip HTML-ish content or trim the applied reply; the reply
        # was still published, so a differing replyText must not raise.
        response = Mock(ok=True, status_code=200)
        response.json.return_value = {"result": {"replyText": "normalized text"}}
        request.return_value = response

        reply_to_review(Mock(token="access-token"), "play-1", "original <b>text</b>")

    @patch("providers.playstore._package_name", return_value="com.example.app")
    @patch("providers.playstore.request_with_retries")
    def test_missing_reply_result_still_raises(self, request, package_name):
        for body in ({}, {"result": {}}, {"result": {"replyText": "  "}}):
            response = Mock(ok=True, status_code=200)
            response.json.return_value = body
            request.return_value = response

            with self.assertRaises(RuntimeError):
                reply_to_review(Mock(token="access-token"), "play-1", "Thanks")


if __name__ == "__main__":
    unittest.main()
