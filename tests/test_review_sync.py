import unittest
from unittest.mock import patch

from common.review_sync import collect_new_reviews, select_new_reviews


def review(review_id: str) -> dict:
    return {"id": review_id}


def _id(item: dict) -> str:
    return item["id"]


class BoundarySelectionTests(unittest.TestCase):
    def test_google_scan_does_not_shadow_review_below_edited_boundary(self):
        # Google sorts by lastModified: the boundary review was edited and now
        # sorts above a genuinely-new review. Without stop_at_boundary the new
        # review must still be selected.
        reviews = [review("boundary"), review("new"), review("old")]
        state = {
            "last_review_id": "boundary",
            "reviews": {"boundary": {}},
            "posted_ids": ["boundary", "old"],
        }

        result = select_new_reviews(
            reviews, state, initial_sync=False, initial_count=5,
            review_id_getter=_id, stop_at_boundary=False,
        )

        self.assertEqual([item["id"] for item in result], ["new"])

    def test_apple_scan_still_stops_at_boundary(self):
        reviews = [review("new"), review("boundary"), review("below")]
        state = {
            "last_review_id": "boundary",
            "reviews": {"boundary": {}},
            "posted_ids": ["boundary"],
        }

        result = select_new_reviews(
            reviews, state, initial_sync=False, initial_count=5,
            review_id_getter=_id, stop_at_boundary=True,
        )

        self.assertEqual([item["id"] for item in result], ["new"])

    def test_initial_sync_baselines_whole_window_so_second_run_posts_only_new(self):
        # Google (no boundary stop): initial sync publishes the newest 5 but
        # must mark EVERY fetched id as seen, otherwise the second run would
        # treat the rest of the 7-day window as "new" and flood the dashboard.
        state = {"last_review_id": None, "posted_ids": [], "reviews": {}}
        window = [review(f"r{n}") for n in range(9, 0, -1)]  # r9 newest .. r1

        with patch("common.review_sync.save_state"):
            entries = collect_new_reviews(
                "playstore", window, state, initial_sync=True, initial_count=5,
                review_id_getter=_id, normalizer=lambda r, s: {"review_id": r["id"]},
                reply_sent_key="google_reply_sent",
                stop_at_boundary=False, baseline_all_fetched=True,
            )
        self.assertEqual(len(entries), 5)  # newest 5 published
        self.assertEqual(set(state["posted_ids"]), {f"r{n}" for n in range(1, 10)})

        # Second run: window now also holds new reviews r10 and r11.
        window2 = [review(f"r{n}") for n in range(11, 0, -1)]
        with patch("common.review_sync.save_state"):
            entries2 = collect_new_reviews(
                "playstore", window2, state, initial_sync=False, initial_count=5,
                review_id_getter=_id, normalizer=lambda r, s: {"review_id": r["id"]},
                reply_sent_key="google_reply_sent",
                stop_at_boundary=False, baseline_all_fetched=True,
            )
        self.assertEqual([e["review_id"] for e in entries2], ["r10", "r11"])  # old r1-r4 never published


if __name__ == "__main__":
    unittest.main()
