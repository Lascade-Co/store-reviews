import os
import unittest
from unittest.mock import patch

import post_reply
from common.review_sync import reply_hash


class ResolveItemsTests(unittest.TestCase):
    def env(self, **kw):
        return patch.dict(os.environ, kw, clear=True)

    def test_single_mode(self):
        with self.env(PLATFORM="appstore", REVIEW_ID="r1", REPLY_TEXT="hi", IS_HUMAN="true"):
            items = post_reply._resolve_items()
        self.assertEqual(
            items,
            [{"platform": "appstore", "review_id": "r1", "reply_text": "hi", "is_human": "true"}],
        )

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


class CaptureCorpusTests(unittest.TestCase):
    ENTRY_INDEX = {
        ("playstore", "r1"): {
            "platform": "playstore",
            "review_id": "r1",
            "body": "Great app, loved the deals",
            "rating": 5,
            "territory_or_language": "en",
        }
    }

    def _item(self, **over):
        item = {"platform": "playstore", "review_id": "r1", "reply_text": "Thanks a lot!", "is_human": True}
        item.update(over)
        return item

    def test_human_send_writes_pending_with_review_words(self):
        with patch.dict(os.environ, {"APP_TYPE": "flight"}, clear=False), \
             patch("post_reply.corpus.write_pending") as write:
            post_reply._capture_corpus(self._item(), self.ENTRY_INDEX, "D1AS")
        write.assert_called_once()
        key, obj = write.call_args.args
        self.assertEqual(key, "D1AS__playstore__r1")
        self.assertEqual(obj["key"], "D1AS__playstore__r1")
        self.assertEqual(obj["review"], "Great app, loved the deals")  # from the dashboard entry
        self.assertEqual(obj["reply"], "Thanks a lot!")
        self.assertEqual(obj["type"], "flight")
        self.assertEqual(obj["rating"], 5)
        self.assertTrue(obj["is_human"])

    def test_non_human_send_is_not_captured(self):
        with patch("post_reply.corpus.write_pending") as write:
            post_reply._capture_corpus(self._item(is_human=False), self.ENTRY_INDEX, "D1AS")
        write.assert_not_called()

    def test_string_is_human_false_is_not_captured(self):
        # The single-mode env value arrives as a string.
        with patch("post_reply.corpus.write_pending") as write:
            post_reply._capture_corpus(self._item(is_human="false"), self.ENTRY_INDEX, "D1AS")
        write.assert_not_called()

    def test_missing_dashboard_entry_is_skipped(self):
        with patch("post_reply.corpus.write_pending") as write:
            post_reply._capture_corpus(self._item(review_id="ghost"), self.ENTRY_INDEX, "D1AS")
        write.assert_not_called()

    def test_write_failure_is_swallowed(self):
        with patch("post_reply.corpus.write_pending", side_effect=RuntimeError("r2 down")) as write:
            post_reply._capture_corpus(self._item(), self.ENTRY_INDEX, "D1AS")  # must not raise
        self.assertEqual(write.call_count, 3)  # retried 3x then gave up


if __name__ == "__main__":
    unittest.main()
