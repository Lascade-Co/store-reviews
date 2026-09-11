import unittest

from merge_state import merge_states


class StateMergeTests(unittest.TestCase):
    def test_merge_preserves_reviews_and_latest_reply_progress(self):
        remote = {
            "last_review_id": "old",
            "last_checked": "2026-07-29T08:00:00+00:00",
            "reviews": {"r1": {"replied_at": "2026-07-29T08:00:00+00:00", "apple_reply_sent": False}},
        }
        local = {
            "last_review_id": "new",
            "last_checked": "2026-07-29T08:01:00+00:00",
            "reviews": {
                "r1": {"replied_at": "2026-07-29T08:05:00+00:00", "apple_reply_sent": True},
                "r2": {"posted_at": "2026-07-29T08:01:00+00:00", "apple_reply_sent": False},
            },
        }

        merged = merge_states(remote, local)

        self.assertEqual(merged["last_review_id"], "new")
        self.assertEqual(merged["reviews"]["r1"]["replied_at"], "2026-07-29T08:05:00+00:00")
        self.assertTrue(merged["reviews"]["r1"]["apple_reply_sent"])
        self.assertIn("r2", merged["reviews"])

    def test_merge_preserves_google_reply_status(self):
        remote = {"reviews": {"r1": {"google_reply_sent": True, "replied_at": "2026-07-29T08:00:00+00:00"}}}
        local = {"reviews": {"r1": {"google_reply_sent": False}}}

        merged = merge_states(remote, local)

        self.assertTrue(merged["reviews"]["r1"]["google_reply_sent"])
        self.assertEqual(merged["reviews"]["r1"]["replied_at"], "2026-07-29T08:00:00+00:00")

    def test_merge_keeps_newest_reply_hash_from_same_snapshot(self):
        remote = {"reviews": {"r1": {"replied_at": "2026-07-29T08:03:00+00:00", "last_sent_reply_hash": "hash-3"}}}
        local = {"reviews": {"r1": {"replied_at": "2026-07-29T08:04:00+00:00", "last_sent_reply_hash": "hash-4"}}}

        merged = merge_states(remote, local)

        self.assertEqual(merged["reviews"]["r1"]["replied_at"], "2026-07-29T08:04:00+00:00")
        self.assertEqual(merged["reviews"]["r1"]["last_sent_reply_hash"], "hash-4")

    def test_merge_unions_posted_ids(self):
        remote = {"posted_ids": ["a", "b"], "reviews": {}}
        local = {"posted_ids": ["b", "c"], "reviews": {}}

        merged = merge_states(remote, local)

        self.assertEqual(merged["posted_ids"], ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()
