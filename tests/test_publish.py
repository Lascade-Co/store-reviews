import unittest
from unittest.mock import patch

from common.publish import build_list, decode_payload, encode_payload, publish
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


class BuildListTests(unittest.TestCase):
    def states(self, appstore_reviews=None, playstore_reviews=None) -> dict:
        return {
            "appstore": {"reviews": appstore_reviews or {}},
            "playstore": {"reviews": playstore_reviews or {}},
        }

    def test_payload_roundtrip_is_plain_json(self):
        payload = {"project_slug": "x", "reviews": [entry("appstore", "r1")]}
        blob = encode_payload(payload)
        self.assertIn('"reviews"', blob)  # plain, human-inspectable JSON
        self.assertEqual(decode_payload(blob), payload)
        self.assertIsNone(decode_payload("not json"))

    def test_keeps_unreplied_previous_and_appends_new(self):
        previous = {"reviews": [entry("appstore", "old1")]}
        states = self.states(appstore_reviews={"old1": {}, "new1": {}})

        result = build_list(previous, states, [entry("appstore", "new1")], "slug")

        ids = {item["review_id"] for item in result["reviews"]}
        self.assertEqual(ids, {"old1", "new1"})
        self.assertEqual(result["project_slug"], "slug")

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

        result = build_list(previous, states, [], "slug")

        ids = {item["review_id"] for item in result["reviews"]}
        self.assertEqual(ids, {"keep1"})

    def test_deduplicates_and_sorts_newest_first(self):
        previous = {"reviews": [entry("appstore", "r1", reviewed_at="2026-09-01T00:00:00+00:00")]}
        states = self.states(appstore_reviews={"r1": {}, "r2": {}})
        new = [
            entry("appstore", "r1"),  # duplicate of previous
            entry("appstore", "r2", reviewed_at="2026-09-03T00:00:00+00:00"),
        ]

        result = build_list(previous, states, new, "slug")

        self.assertEqual([item["review_id"] for item in result["reviews"]], ["r2", "r1"])


class PublishNotifyTests(unittest.TestCase):
    """Slack's count must equal what the dashboard shows: new AND pending only."""

    def _publish(self, new_entries, states):
        with patch("common.publish.download_current", return_value=None), patch(
            "common.publish.upload"
        ), patch("common.publish.notify_slack") as notify:
            publish("slug", states, new_entries)
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

        notify.assert_called_once_with(1, "slug")  # not 2 — the replied one is excluded

    def test_notify_counts_all_when_every_new_review_is_pending(self):
        new_entries = [entry("appstore", "n1"), entry("playstore", "n2")]
        states = {
            "appstore": {"reviews": {"n1": {}}},
            "playstore": {"reviews": {"n2": {}}},
        }

        notify = self._publish(new_entries, states)

        notify.assert_called_once_with(2, "slug")


class CollectNewReviewsTests(unittest.TestCase):
    def test_collect_updates_state_and_returns_entries_without_slack(self):
        state = {"last_review_id": None, "reviews": {}, "posted_ids": []}
        reviews = [{"id": f"r{n}"} for n in range(7, 0, -1)]  # newest first

        with patch("common.review_sync.save_state"):
            entries = collect_new_reviews(
                "appstore",
                reviews,
                state,
                initial_sync=True,
                initial_count=5,
                review_id_getter=lambda r: r["id"],
                normalizer=lambda r, s: {"platform": "appstore", "review_id": r["id"], "suggested_reply": s},
                reply_sent_key="apple_reply_sent",
                # Batch generator: one call with all new reviews -> {id: reply}.
                suggestion_generator=lambda new: {r["id"]: "AI!" for r in new},
            )

        self.assertEqual(len(entries), 5)  # newest 5 on initial sync
        self.assertEqual(entries[0]["suggested_reply"], "AI!")
        self.assertEqual(state["last_review_id"], "r7")
        self.assertEqual(len(state["reviews"]), 5)
        for review_entry in state["reviews"].values():
            self.assertNotIn("slack_ts", review_entry)  # web mode: no Slack thread
        self.assertEqual(len(state["posted_ids"]), 5)

    def test_incremental_collect_respects_boundary_and_dedup(self):
        state = {"last_review_id": "r5", "reviews": {"r5": {}}, "posted_ids": ["r5", "r4"]}
        reviews = [{"id": "r7"}, {"id": "r6"}, {"id": "r5"}, {"id": "r4"}]

        with patch("common.review_sync.save_state"):
            entries = collect_new_reviews(
                "appstore",
                reviews,
                state,
                initial_sync=False,
                initial_count=5,
                review_id_getter=lambda r: r["id"],
                normalizer=lambda r, s: {"platform": "appstore", "review_id": r["id"]},
                reply_sent_key="apple_reply_sent",
                stop_at_boundary=True,
            )

        self.assertEqual([e["review_id"] for e in entries], ["r6", "r7"])  # oldest-first
        self.assertEqual(state["last_review_id"], "r7")


if __name__ == "__main__":
    unittest.main()
