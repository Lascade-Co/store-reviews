import os
import unittest
from unittest.mock import Mock, patch

from providers.appstore import INITIAL_SYNC_COUNT, _review_id, fetch_reviews, normalize_entry
from common.review_sync import select_new_reviews


def review(review_id: str) -> dict:
    return {"id": review_id, "attributes": {"createdDate": review_id, "rating": 5}}


def _new_reviews(reviews, state, initial_sync):
    # Apple uses the shared selection with the boundary stop enabled
    # (createdDate order is immutable, so stopping at last_review_id is safe).
    return select_new_reviews(
        reviews, state, initial_sync, INITIAL_SYNC_COUNT, _review_id, stop_at_boundary=True
    )


class AppStoreSyncTests(unittest.TestCase):
    def test_initial_sync_is_limited_and_resumes_without_duplicates(self):
        reviews = [review(str(number)) for number in range(10, 0, -1)]
        state = {"last_review_id": None, "reviews": {"10": {}, "9": {}}}

        result = _new_reviews(reviews, state, initial_sync=True)

        self.assertEqual([item["id"] for item in result], ["8", "7", "6"])
        self.assertEqual(INITIAL_SYNC_COUNT, 5)

    def test_incremental_sync_stops_at_last_review(self):
        reviews = [review("12"), review("11"), review("10"), review("9"), review("8")]
        state = {"last_review_id": "10", "reviews": {"10": {}, "9": {}, "8": {}}}

        result = _new_reviews(reviews, state, initial_sync=False)

        self.assertEqual([item["id"] for item in result], ["12", "11"])

    def test_new_reviews_dedup_via_posted_ids(self):
        # "2" was posted earlier and pruned from reviews, but survives in posted_ids,
        # so it must not be re-selected even though it is not in the reviews map.
        reviews = [review("3"), review("2"), review("1")]
        state = {"last_review_id": "1", "reviews": {}, "posted_ids": ["2", "1"]}

        result = _new_reviews(reviews, state, initial_sync=False)

        self.assertEqual([item["id"] for item in result], ["3"])

    def test_normalize_entry_shapes_dashboard_fields(self):
        entry = normalize_entry(
            {
                "id": "r1",
                "attributes": {
                    "rating": 2,
                    "title": " Bad price ",
                    "body": "Too costly",
                    "reviewerNickname": "Sam",
                    "territory": "IND",
                    "createdDate": "2026-09-01T00:00:00Z",
                },
            },
            "Sorry to hear!",
        )
        self.assertEqual(entry["platform"], "appstore")
        self.assertEqual(entry["review_id"], "r1")
        self.assertEqual(entry["title"], "Bad price")
        self.assertEqual(entry["suggested_reply"], "Sorry to hear!")
        self.assertFalse(entry["replied"])

    @patch.dict(os.environ, {"APPSTORE_APPLE_ID": "123"})
    @patch("providers.appstore.request_with_retries")
    def test_fetch_reviews_follows_pagination(self, request):
        page1 = Mock()
        page1.raise_for_status.return_value = None
        page1.json.return_value = {"data": [review("2"), review("1")], "links": {"next": "https://api/next"}}
        page2 = Mock()
        page2.raise_for_status.return_value = None
        page2.json.return_value = {"data": [review("0")], "links": {}}
        request.side_effect = [page1, page2]

        result = fetch_reviews("token")

        self.assertEqual(request.call_count, 2)
        self.assertEqual({item["id"] for item in result}, {"2", "1", "0"})

    @patch.dict(os.environ, {"APPSTORE_APPLE_ID": "123"})
    @patch("providers.appstore.request_with_retries")
    def test_fetch_reviews_stops_at_boundary(self, request):
        page1 = Mock()
        page1.raise_for_status.return_value = None
        page1.json.return_value = {"data": [review("5"), review("4")], "links": {"next": "https://api/next"}}
        request.side_effect = [page1]

        result = fetch_reviews("token", stop_at_id="4")

        self.assertEqual(request.call_count, 1)  # stopped without fetching page 2
        self.assertEqual({item["id"] for item in result}, {"5", "4"})


if __name__ == "__main__":
    unittest.main()
