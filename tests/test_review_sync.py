import unittest
from unittest.mock import Mock, patch

from common.review_sync import collect_new_reviews, select_new_reviews


def review(review_id: str) -> dict:
    return {"id": review_id}


def _id(item: dict) -> str:
    return item["id"]


class AutoReplyAndPingTests(unittest.TestCase):
    """The incremental loop auto-sends gated positives and collects pings."""

    def _collect(self, *, suggestion, rating, auto_enabled, reply_sender=None, ping_sink=None):
        state = {"last_review_id": "r0", "reviews": {"r0": {}}, "posted_ids": ["r0"]}
        reviews = [review("r1"), review("r0")]  # r1 is new, r0 is the boundary

        def normalizer(r, s):
            return {
                "platform": "playstore",
                "review_id": r["id"],
                "rating": rating,
                "title": None,
                "body": "Great app",
                "suggested_reply": s,
                "replied": False,
                "reply_text": None,
                "auto_replied": False,
            }

        with patch("common.review_sync.save_state"):
            entries = collect_new_reviews(
                "playstore", reviews, state, initial_sync=False,
                review_id_getter=_id, normalizer=normalizer,
                reply_sent_key="google_reply_sent", stop_at_boundary=False,
                suggestion_generator=lambda new: {"r1": suggestion},
                reply_sender=reply_sender, auto_reply_enabled=auto_enabled,
                ping_sink=ping_sink,
            )
        return entries, state

    def test_auto_sends_and_records_when_gated_conditions_met(self):
        sender = Mock()
        entries, state = self._collect(
            suggestion={"reply": "Thanks!", "auto_reply": True, "ping": False},
            rating=5, auto_enabled=True, reply_sender=sender,
        )
        sender.assert_called_once_with("r1", "Thanks!")
        self.assertTrue(entries[0]["auto_replied"])
        self.assertTrue(entries[0]["replied"])
        self.assertEqual(entries[0]["reply_text"], "Thanks!")
        recorded = state["reviews"]["r1"]
        self.assertTrue(recorded["auto_replied"])
        self.assertTrue(recorded["google_reply_sent"])
        self.assertIn("last_sent_reply_hash", recorded)
        self.assertIn("replied_at", recorded)

    def test_not_sent_below_rating_floor(self):
        sender = Mock()
        entries, state = self._collect(
            suggestion={"reply": "Thanks!", "auto_reply": True, "ping": False},
            rating=2, auto_enabled=True, reply_sender=sender,
        )
        sender.assert_not_called()
        self.assertFalse(entries[0]["auto_replied"])
        self.assertFalse(state["reviews"]["r1"].get("auto_replied"))

    def test_not_sent_when_app_not_opted_in(self):
        sender = Mock()
        self._collect(
            suggestion={"reply": "Thanks!", "auto_reply": True, "ping": False},
            rating=5, auto_enabled=False, reply_sender=sender,
        )
        sender.assert_not_called()

    def test_not_sent_when_codex_flag_false(self):
        sender = Mock()
        self._collect(
            suggestion={"reply": "Thanks!", "auto_reply": False, "ping": False},
            rating=5, auto_enabled=True, reply_sender=sender,
        )
        sender.assert_not_called()

    def test_send_failure_leaves_review_pending(self):
        sender = Mock(side_effect=RuntimeError("store rejected"))
        entries, state = self._collect(
            suggestion={"reply": "Thanks!", "auto_reply": True, "ping": False},
            rating=5, auto_enabled=True, reply_sender=sender,
        )
        sender.assert_called_once()
        self.assertFalse(entries[0]["auto_replied"])
        recorded = state["reviews"]["r1"]
        self.assertFalse(recorded.get("auto_replied"))
        self.assertFalse(recorded.get("google_reply_sent"))
        self.assertNotIn("last_sent_reply_hash", recorded)

    def test_ping_flagged_review_is_collected(self):
        sink = []
        self._collect(
            suggestion={"reply": "We'll look into it", "auto_reply": False, "ping": True},
            rating=3, auto_enabled=False, ping_sink=sink,
        )
        self.assertEqual(len(sink), 1)
        self.assertEqual(sink[0]["review_id"], "r1")
        self.assertEqual(sink[0]["platform"], "playstore")


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
            reviews, state, review_id_getter=_id, stop_at_boundary=False,
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
            reviews, state, review_id_getter=_id, stop_at_boundary=True,
        )

        self.assertEqual([item["id"] for item in result], ["new"])

    def test_first_run_baselines_and_posts_nothing_then_only_new_after(self):
        # First run posts NOTHING — it just records every existing review as seen
        # and marks the app baselined. Only reviews that arrive AFTER connection
        # are posted on later runs.
        state = {"posted_ids": [], "reviews": {}}
        window = [review(f"r{n}") for n in range(9, 0, -1)]  # r9 newest .. r1

        with patch("common.review_sync.save_state"):
            entries = collect_new_reviews(
                "playstore", window, state, initial_sync=True,
                review_id_getter=_id, normalizer=lambda r, s: {"review_id": r["id"]},
                reply_sent_key="google_reply_sent", stop_at_boundary=False,
            )
        self.assertEqual(entries, [])  # nothing published on the baseline run
        self.assertTrue(state["baselined"])
        self.assertEqual(set(state["posted_ids"]), {f"r{n}" for n in range(1, 10)})

        # Second run: window now also holds new reviews r10 and r11.
        window2 = [review(f"r{n}") for n in range(11, 0, -1)]
        with patch("common.review_sync.save_state"):
            entries2 = collect_new_reviews(
                "playstore", window2, state, initial_sync=False,
                review_id_getter=_id, normalizer=lambda r, s: {"review_id": r["id"]},
                reply_sent_key="google_reply_sent", stop_at_boundary=False,
            )
        self.assertEqual([e["review_id"] for e in entries2], ["r10", "r11"])  # old r1-r9 never published

    def test_first_run_with_zero_reviews_still_posts_the_first_review_later(self):
        # App connected while it had no reviews: the baseline run sets the flag
        # even with an empty window, so the first real review is NOT missed.
        state = {"posted_ids": [], "reviews": {}}
        with patch("common.review_sync.save_state"):
            entries = collect_new_reviews(
                "playstore", [], state, initial_sync=True,
                review_id_getter=_id, normalizer=lambda r, s: {"review_id": r["id"]},
                reply_sent_key="google_reply_sent", stop_at_boundary=False,
            )
        self.assertEqual(entries, [])
        self.assertTrue(state["baselined"])
        self.assertIsNone(state.get("last_review_id"))

        # A review now appears; the run is incremental (baselined) and posts it.
        with patch("common.review_sync.save_state"):
            entries2 = collect_new_reviews(
                "playstore", [review("r1")], state, initial_sync=False,
                review_id_getter=_id, normalizer=lambda r, s: {"review_id": r["id"]},
                reply_sent_key="google_reply_sent", stop_at_boundary=False,
            )
        self.assertEqual([e["review_id"] for e in entries2], ["r1"])


if __name__ == "__main__":
    unittest.main()
