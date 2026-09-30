import os
import unittest
from unittest.mock import patch

import post_reply
from common.review_sync import reply_hash


class ResolveItemsTests(unittest.TestCase):
    def env(self, **kw):
        return patch.dict(os.environ, kw, clear=True)

    def test_single_mode(self):
        with self.env(PLATFORM="appstore", REVIEW_ID="r1", REPLY_TEXT="hi"):
            items = post_reply._resolve_items()
        self.assertEqual(items, [{"platform": "appstore", "review_id": "r1", "reply_text": "hi"}])

    def test_batch_mode_from_replies_json(self):
        blob = '[{"platform":"playstore","review_id":"r2","reply_text":"yo"}]'
        with self.env(REPLIES=blob):
            items = post_reply._resolve_items()
        self.assertEqual(items, [{"platform": "playstore", "review_id": "r2", "reply_text": "yo"}])

    def test_reply_all_flag_with_empty_replies_is_error(self):
        with self.env(REPLY_ALL="true", REPLIES=""):
            self.assertIsNone(post_reply._resolve_items())

    def test_invalid_json_is_error(self):
        with self.env(REPLIES="{not json"):
            self.assertIsNone(post_reply._resolve_items())

    def test_replies_must_be_a_list(self):
        with self.env(REPLIES='{"platform":"appstore"}'):
            self.assertIsNone(post_reply._resolve_items())


class SendOneTests(unittest.TestCase):
    def test_sent_updates_state_and_calls_store(self):
        states = {"appstore": {"reviews": {}}, "playstore": {"reviews": {"r1": {}}}}
        calls = []
        with patch("post_reply.save_state"):
            result = post_reply._send_one(
                states, "playstore", "r1", "hello", lambda p, rid, t: calls.append((p, rid, t))
            )
        self.assertEqual(result, "sent")
        entry = states["playstore"]["reviews"]["r1"]
        self.assertTrue(entry["google_reply_sent"])
        self.assertEqual(entry["last_sent_reply_hash"], reply_hash("hello"))
        self.assertIn("replied_at", entry)
        self.assertEqual(calls, [("playstore", "r1", "hello")])

    def test_skipped_on_identical_hash_does_not_resend(self):
        states = {
            "appstore": {"reviews": {}},
            "playstore": {"reviews": {"r1": {"last_sent_reply_hash": reply_hash("hello")}}},
        }

        def boom(*_):
            raise AssertionError("must not send an identical reply again")

        with patch("post_reply.save_state"):
            result = post_reply._send_one(states, "playstore", "r1", "hello", boom)
        self.assertEqual(result, "skipped")

    def test_skipped_when_review_not_active(self):
        # A review not in state (already handled elsewhere and pruned, or expired)
        # is a benign no-op — skipped, not failed, so it doesn't sink a batch.
        states = {"appstore": {"reviews": {}}, "playstore": {"reviews": {}}}
        with patch("post_reply.save_state"):
            result = post_reply._send_one(states, "playstore", "missing", "hi", lambda *a: None)
        self.assertEqual(result, "skipped")

    def test_failed_on_unknown_platform(self):
        states = {"appstore": {"reviews": {}}, "playstore": {"reviews": {}}}
        with patch("post_reply.save_state"):
            result = post_reply._send_one(states, "windows", "r1", "hi", lambda *a: None)
        self.assertEqual(result, "failed")


if __name__ == "__main__":
    unittest.main()
