import os
import unittest
from unittest.mock import Mock, patch

from common.publish import (
    build_list,
    decode_payload,
    encode_payload,
    notify_developer,
    notify_slack,
    publish,
)
from common.review_sync import collect_new_reviews


def entry(platform: str, review_id: str, **overrides) -> dict:
    base = {
        "platform": platform,
        "review_id": review_id,
        "rating": 4,
        "title": None,
        "body": "text",
        "reviewer": "A",
        "territory_or_language": "en",
        "reviewed_at": f"2026-09-0{review_id[-1] if review_id[-1].isdigit() else '1'}T00:00:00+00:00",
        "detected_at": "2026-09-04T00:00:00+00:00",
        "suggested_reply": "Thanks!",
        "replied": False,
        "reply_text": None,
    }
    base.update(overrides)
    return base
  

APP = {"appname": "App Name", "appcode": "appcode", "infisical_slug": "slug"}


class BuildListTests(unittest.TestCase):
    def states(self, appstore_reviews=None, playstore_reviews=None) -> dict:
        return {
            "appstore": {"reviews": appstore_reviews or {}},
            "playstore": {"reviews": playstore_reviews or {}},
        }

    def test_payload_roundtrip_is_plain_json(self):
        payload = {"app_details": APP, "reviews": [entry("appstore", "r1")]}
        blob = encode_payload(payload)
        self.assertIn('"reviews"', blob)  # plain, human-inspectable JSON
        self.assertEqual(decode_payload(blob), payload)
        self.assertIsNone(decode_payload("not json"))

    def test_keeps_unreplied_previous_and_appends_new(self):
        previous = {"reviews": [entry("appstore", "old1")]}
        states = self.states(appstore_reviews={"old1": {}, "new1": {}})

        result = build_list(previous, states, [entry("appstore", "new1")], APP)

        ids = {item["review_id"] for item in result["reviews"]}
        self.assertEqual(ids, {"old1", "new1"})
        self.assertEqual(result["app_details"], APP)
        self.assertEqual(result["app_details"]["appcode"], "appcode")

    def test_drops_replied_and_pruned_entries(self):
        previous = {
            "reviews": [
                entry("appstore", "replied1"),
                entry("appstore", "pruned1"),
                entry("playstore", "console1"),
                entry("appstore", "keep1"),
            ]
        }
        states = self.states(
            appstore_reviews={
                "replied1": {"last_sent_reply_hash": "abc"},
                "keep1": {},
                # pruned1 absent from state entirely
            },
            playstore_reviews={"console1": {"google_reply_sent": True}},
        )

        result = build_list(previous, states, [], APP)

        ids = {item["review_id"] for item in result["reviews"]}
        self.assertEqual(ids, {"keep1"})

    def test_reconciles_stale_auto_replied_entry_from_state(self):
        # Entry (carried from a stale R2 file) says not replied, but state is the
        # authority and says it was auto-replied. Output must reflect state so the
        # dashboard shows the badge, not an editable Reply button (no duplicate).
        stale = entry("playstore", "r1", auto_replied=False, replied=False, reply_text=None)
        states = {
            "appstore": {"reviews": {}},
            "playstore": {
                "reviews": {
                    "r1": {
                        "auto_replied": True,
                        "google_reply_sent": True,
                        "last_sent_reply_hash": "h",
                    }
                }
            },
        }

        result = build_list({"reviews": [stale]}, states, [], APP)

        self.assertEqual(len(result["reviews"]), 1)
        self.assertTrue(result["reviews"][0]["auto_replied"])
        self.assertTrue(result["reviews"][0]["replied"])

    def test_deduplicates_and_sorts_newest_first(self):
        previous = {"reviews": [entry("appstore", "r1", reviewed_at="2026-09-01T00:00:00+00:00")]}
        states = self.states(appstore_reviews={"r1": {}, "r2": {}})
        new = [
            entry("appstore", "r1"),  # duplicate of previous
            entry("appstore", "r2", reviewed_at="2026-09-03T00:00:00+00:00"),
        ]

        result = build_list(previous, states, new, APP)

        self.assertEqual([item["review_id"] for item in result["reviews"]], ["r2", "r1"])


class PublishNotifyTests(unittest.TestCase):
    """Slack's count must equal what the dashboard shows: new AND pending only."""

    def _publish(self, new_entries, states):
        with patch("common.publish.download_current", return_value=None), patch(
            "common.publish.upload"
        ), patch("common.publish.notify_slack") as notify:
            publish(APP, states, new_entries)
        return notify

    def test_notify_counts_only_pending_new_reviews(self):
        # Two freshly-fetched reviews, but "answered1" already had a store reply
        # (its reply_sent flag is set) so it is filtered off the dashboard.
        new_entries = [entry("appstore", "new1"), entry("appstore", "answered1")]
        states = {
            "appstore": {
                "reviews": {
                    "new1": {},
                    "answered1": {"apple_reply_sent": True},
                }
            },
            "playstore": {"reviews": {}},
        }

        notify = self._publish(new_entries, states)

        notify.assert_called_once_with(1, APP, 0)  # not 2 — the replied one is excluded

    def test_notify_counts_all_when_every_new_review_is_pending(self):
        new_entries = [entry("appstore", "n1"), entry("playstore", "n2")]
        states = {
            "appstore": {"reviews": {"n1": {}}},
            "playstore": {"reviews": {"n2": {}}},
        }

        notify = self._publish(new_entries, states)

        notify.assert_called_once_with(2, APP, 0)

    def test_auto_replied_entry_stays_pending_and_is_counted(self):
        # An auto-replied review: its state carries auto_replied + a reply hash,
        # yet it must remain on the dashboard (badge) and be counted as auto.
        new_entries = [
            entry("playstore", "auto1", auto_replied=True, replied=True, reply_text="Thanks!"),
        ]
        states = {
            "appstore": {"reviews": {}},
            "playstore": {
                "reviews": {
                    "auto1": {
                        "auto_replied": True,
                        "google_reply_sent": True,
                        "last_sent_reply_hash": "abc",
                    }
                }
            },
        }

        notify = self._publish(new_entries, states)

        notify.assert_called_once_with(1, APP, 1)  # pending=1, auto=1


class CollectNewReviewsTests(unittest.TestCase):
    def test_first_run_baselines_and_returns_no_entries(self):
        state = {"reviews": {}, "posted_ids": []}
        reviews = [{"id": f"r{n}"} for n in range(7, 0, -1)]  # newest first

        with patch("common.review_sync.save_state"):
            entries = collect_new_reviews(
                "appstore",
                reviews,
                state,
                initial_sync=True,
                review_id_getter=lambda r: r["id"],
                normalizer=lambda r, s: {"platform": "appstore", "review_id": r["id"], "suggested_reply": s},
                reply_sent_key="apple_reply_sent",
                suggestion_generator=lambda new: {r["id"]: "AI!" for r in new},
            )

        self.assertEqual(entries, [])  # nothing posted on the baseline run
        self.assertTrue(state["baselined"])
        self.assertEqual(state["last_review_id"], "r7")
        self.assertEqual(state["reviews"], {})  # no reviews recorded as pending
        self.assertEqual(set(state["posted_ids"]), {f"r{n}" for n in range(1, 8)})

    def test_incremental_collect_respects_boundary_and_dedup(self):
        state = {"last_review_id": "r5", "reviews": {"r5": {}}, "posted_ids": ["r5", "r4"]}
        reviews = [{"id": "r7"}, {"id": "r6"}, {"id": "r5"}, {"id": "r4"}]

        with patch("common.review_sync.save_state"):
            entries = collect_new_reviews(
                "appstore",
                reviews,
                state,
                initial_sync=False,
                review_id_getter=lambda r: r["id"],
                normalizer=lambda r, s: {"platform": "appstore", "review_id": r["id"]},
                reply_sent_key="apple_reply_sent",
                stop_at_boundary=True,
            )

        self.assertEqual([e["review_id"] for e in entries], ["r6", "r7"])  # oldest-first
        self.assertEqual(state["last_review_id"], "r7")


class NotifySlackMessageTests(unittest.TestCase):
    def test_message_includes_auto_and_manual_counts(self):
        inst = Mock()
        with patch.dict(os.environ, {"SITE_BASE_URL": "https://x.example"}), patch(
            "common.publish.SlackClient", return_value=inst
        ):
            notify_slack(3, APP, 1)
        text = inst.post_review.call_args.args[0]
        self.assertIn("*New Reviews:* 3", text)
        self.assertIn("*Auto-replied:* 1", text)
        self.assertIn("*Needs manual reply:* 2", text)

    def test_no_message_when_no_new_reviews(self):
        with patch("common.publish.SlackClient") as slack:
            notify_slack(0, APP, 0)
        slack.assert_not_called()


class DevPingTests(unittest.TestCase):
    PINGS = [
        {"review_id": "r1", "platform": "playstore", "rating": 2, "title": None, "body": "crashes on launch"},
    ]

    def test_skips_when_no_dev_id(self):
        with patch.dict(os.environ, {}, clear=False), patch("common.publish.SlackClient") as slack:
            os.environ.pop("SLACK_DEV_ID", None)
            notify_developer(APP, self.PINGS)
        slack.assert_not_called()

    def test_skips_when_no_pings(self):
        with patch.dict(os.environ, {"SLACK_DEV_ID": "U123"}), patch("common.publish.SlackClient") as slack:
            notify_developer(APP, [])
        slack.assert_not_called()

    def test_posts_mention_and_body_when_dev_id_present(self):
        inst = Mock()
        with patch.dict(os.environ, {"SLACK_DEV_ID": "U123"}), patch(
            "common.publish.SlackClient", return_value=inst
        ) as slack:
            notify_developer(APP, self.PINGS)
        slack.assert_called_once()
        text = inst.post_review.call_args.args[0]
        self.assertIn("<@U123>", text)
        self.assertIn("crashes on launch", text)

    def test_slack_failure_is_swallowed(self):
        inst = Mock()
        inst.post_review.side_effect = RuntimeError("boom")
        with patch.dict(os.environ, {"SLACK_DEV_ID": "U123"}), patch(
            "common.publish.SlackClient", return_value=inst
        ):
            notify_developer(APP, self.PINGS)  # must not raise


if __name__ == "__main__":
    unittest.main()
