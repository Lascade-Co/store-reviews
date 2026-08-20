import unittest
from unittest.mock import Mock, patch

from common.review_sync import (
    decode_slack_text,
    post_new_reviews,
    reply_hash,
    select_new_reviews,
    sync_slack_replies,
)


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
    def setUp(self):
        # sync_slack_replies persists state via save_state; mock it so tests
        # never write real files into the repo's state/ folder.
        patcher = patch("common.review_sync.save_state")
        patcher.start()
        self.addCleanup(patcher.stop)

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

    def test_initial_sync_baselines_whole_window_so_second_run_posts_only_new(self):
        # Google (no boundary stop): initial sync posts the newest 5 but must
        # mark EVERY fetched id as seen, otherwise the second run would treat
        # the rest of the 7-day window as "new" and flood Slack with old reviews.
        slack = Mock()
        slack.post_review.side_effect = lambda text: f"ts-{text}"
        state = {"last_review_id": None, "posted_ids": [], "reviews": {}}
        window = [review(f"r{n}") for n in range(9, 0, -1)]  # r9 newest .. r1

        with patch("common.review_sync.save_state"):
            post_new_reviews(
                "playstore", window, state, slack, initial_sync=True,
                initial_count=5, review_id_getter=_id, formatter=_id,
                reply_sent_key="google_reply_sent",
                stop_at_boundary=False, baseline_all_fetched=True,
            )
        self.assertEqual(slack.post_review.call_count, 5)  # newest 5 posted
        self.assertEqual(set(state["posted_ids"]), {f"r{n}" for n in range(1, 10)})

        # Second run: window now also holds new reviews r10 and r11.
        slack.post_review.reset_mock()
        window2 = [review(f"r{n}") for n in range(11, 0, -1)]
        with patch("common.review_sync.save_state"):
            post_new_reviews(
                "playstore", window2, state, slack, initial_sync=False,
                initial_count=5, review_id_getter=_id, formatter=_id,
                reply_sent_key="google_reply_sent",
                stop_at_boundary=False, baseline_all_fetched=True,
            )
        posted = [call.args[0] for call in slack.post_review.call_args_list]
        self.assertEqual(posted, ["r10", "r11"])  # old r1-r4 never posted

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
