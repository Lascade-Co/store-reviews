import unittest
from unittest.mock import Mock

from common.review_sync import decode_slack_text, reply_hash, select_new_reviews, sync_slack_replies


def review(review_id: str) -> dict:
    return {"id": review_id}


def _id(item: dict) -> str:
    return item["id"]


class DecodeSlackTextTests(unittest.TestCase):
    def test_entities_are_unescaped(self):
        self.assertEqual(decode_slack_text("Thanks &amp; sorry"), "Thanks & sorry")
        self.assertEqual(decode_slack_text("a &lt; b &gt; c"), "a < b > c")

    def test_amp_is_unescaped_last_so_markup_is_not_reintroduced(self):
        # "&amp;lt;" is a literal "&lt;" typed by the user, not a "<".
        self.assertEqual(decode_slack_text("&amp;lt;"), "&lt;")

    def test_labelled_link_keeps_label(self):
        self.assertEqual(decode_slack_text("<https://x.com|x.com>"), "x.com")

    def test_bare_link_keeps_url(self):
        self.assertEqual(decode_slack_text("<https://x.com>"), "https://x.com")

    def test_mentions_and_commands_are_unwrapped(self):
        self.assertEqual(decode_slack_text("<@U1|alice>"), "alice")
        self.assertEqual(decode_slack_text("hi <@U123>"), "hi U123")
        self.assertEqual(decode_slack_text("<#C123|general>"), "general")
        self.assertEqual(decode_slack_text("<!here>"), "here")

    def test_plain_text_is_unchanged(self):
        self.assertEqual(decode_slack_text("Just a normal reply."), "Just a normal reply.")


class SyncRepliesDecodeTests(unittest.TestCase):
    def test_store_reply_and_hash_use_decoded_text(self):
        slack = Mock()
        slack.is_human_message.side_effect = lambda message: message.get("user") == "U1"
        send_reply = Mock()
        state = {"reviews": {"r1": {"slack_ts": "123.456"}}}
        slack.replies.return_value = [
            {"ts": "123.456", "user": "UBOT", "text": "review"},
            {"ts": "123.500", "user": "U1", "text": "Thanks &amp; sorry, see <https://x.com|x.com>"},
        ]

        sync_slack_replies("playstore", state, slack, "google_reply_sent", send_reply)

        send_reply.assert_called_once_with("r1", "Thanks & sorry, see x.com")
        self.assertEqual(
            state["reviews"]["r1"]["last_sent_reply_hash"],
            reply_hash("Thanks & sorry, see x.com"),
        )

    def test_reply_empty_after_decoding_is_skipped(self):
        slack = Mock()
        slack.is_human_message.side_effect = lambda message: message.get("user") == "U1"
        send_reply = Mock()
        state = {"reviews": {"r1": {"slack_ts": "123.456"}}}
        slack.replies.return_value = [
            {"ts": "123.456", "user": "UBOT", "text": "review"},
            {"ts": "123.500", "user": "U1", "text": "<> "},
        ]

        sync_slack_replies("playstore", state, slack, "google_reply_sent", send_reply)

        send_reply.assert_not_called()
        self.assertNotIn("last_sent_reply_hash", state["reviews"]["r1"])


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


if __name__ == "__main__":
    unittest.main()
