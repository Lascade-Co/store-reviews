import json
import os
import unittest
from unittest.mock import Mock, patch

import corpus_action


class FeedRecordsTests(unittest.TestCase):
    def test_embeds_and_upserts_with_generated_key_and_defaults(self):
        embed = Mock(return_value=[[0.1, 0.2, 0.3]])
        upsert = Mock()

        count = corpus_action.feed_records(
            [{"review": "Loved the flight deals", "reply": "Thanks!", "app": "D1AS_D1IS", "type": "flight", "rating": 5}],
            embed_fn=embed, upsert_fn=upsert,
        )

        self.assertEqual(count, 1)
        embed.assert_called_once_with(["Loved the flight deals"])
        row = upsert.call_args.args[0]
        self.assertEqual(row["vector"], [0.1, 0.2, 0.3])
        self.assertEqual(row["review"], "Loved the flight deals")
        self.assertEqual(row["reply"], "Thanks!")
        self.assertEqual(row["app"], "D1AS_D1IS")
        self.assertEqual(row["type"], "flight")
        self.assertEqual(row["rating"], 5)
        self.assertEqual(row["store"], "manual")  # default marker for fed rows
        self.assertTrue(row["key"].startswith("feed__flight__"))
        self.assertTrue(row["created_at"])  # auto-stamped
        self.assertIn("model", row)

    def test_generated_key_is_stable_for_the_same_review(self):
        k1 = corpus_action._feed_key("same text", "flight")
        k2 = corpus_action._feed_key("same text", "flight")
        self.assertEqual(k1, k2)
        self.assertNotEqual(k1, corpus_action._feed_key("other text", "flight"))

    def test_explicit_key_store_and_lang_are_respected(self):
        upsert = Mock()
        corpus_action.feed_records(
            [{
                "review": "r", "reply": "y", "app": "A", "type": "car",
                "key": "mykey", "store": "playstore", "lang": "es", "rating": 1,
            }],
            embed_fn=Mock(return_value=[[0.0]]), upsert_fn=upsert,
        )
        row = upsert.call_args.args[0]
        self.assertEqual(row["key"], "mykey")
        self.assertEqual(row["store"], "playstore")
        self.assertEqual(row["lang"], "es")
        self.assertEqual(row["rating"], 1)

    def test_missing_required_field_raises(self):
        with self.assertRaises(ValueError):
            corpus_action.feed_records(
                [{"review": "r", "reply": "y", "app": "A"}],  # no type
                embed_fn=Mock(), upsert_fn=Mock(),
            )

    def test_multiple_records_all_upserted(self):
        upsert = Mock()
        count = corpus_action.feed_records(
            [
                {"review": "r1", "reply": "a", "app": "A", "type": "flight"},
                {"review": "r2", "reply": "b", "app": "A", "type": "flight"},
            ],
            embed_fn=Mock(return_value=[[0.1]]), upsert_fn=upsert,
        )
        self.assertEqual(count, 2)
        self.assertEqual(upsert.call_count, 2)


class ParsePayloadTests(unittest.TestCase):
    def test_parses_an_array(self):
        self.assertEqual(len(corpus_action.parse_payload('[{"review":"r"}]')), 1)

    def test_tolerates_a_single_object(self):
        records = corpus_action.parse_payload('{"review":"r","reply":"y"}')
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["review"], "r")

    def test_rejects_a_bare_scalar(self):
        with self.assertRaises(ValueError):
            corpus_action.parse_payload('"nope"')


class RunFeedTests(unittest.TestCase):
    def test_stamps_current_app_and_type_onto_records(self):
        with patch("corpus_action.feed_records", return_value=1) as feed:
            rc = corpus_action.run_feed(
                "D1AS_D1IS", "flight",
                json.dumps([{"review": "great deals", "reply": "thanks", "rating": 5}]),
            )
        self.assertEqual(rc, 0)
        records = feed.call_args.args[0]
        self.assertEqual(records[0]["app"], "D1AS_D1IS")  # stamped from APP_CODE
        self.assertEqual(records[0]["type"], "flight")     # stamped from APP_TYPE

    def test_record_type_override_is_kept(self):
        with patch("corpus_action.feed_records", return_value=1) as feed:
            corpus_action.run_feed(
                "A", "flight",
                json.dumps([{"review": "r", "reply": "y", "type": "hotel"}]),
            )
        self.assertEqual(feed.call_args.args[0][0]["type"], "hotel")

    def test_empty_payload_is_error(self):
        self.assertEqual(corpus_action.run_feed("A", "flight", ""), 1)

    def test_bad_json_is_error(self):
        self.assertEqual(corpus_action.run_feed("A", "flight", "{not json"), 1)

    def test_missing_app_is_error(self):
        self.assertEqual(corpus_action.run_feed("", "flight", "[]"), 1)


class MainDispatchTests(unittest.TestCase):
    def test_clear_calls_corpus_clear(self):
        with patch.dict(os.environ, {"CORPUS_ACTION": "clear"}, clear=True), \
             patch("corpus_action.corpus.clear") as clear:
            rc = corpus_action.main()
        self.assertEqual(rc, 0)
        clear.assert_called_once_with()

    def test_feed_routes_to_run_feed(self):
        env = {"CORPUS_ACTION": "feed", "APP_CODE": "A", "APP_TYPE": "flight", "PAYLOAD": "[]"}
        with patch.dict(os.environ, env, clear=True), \
             patch("corpus_action.run_feed", return_value=0) as run:
            rc = corpus_action.main()
        self.assertEqual(rc, 0)
        run.assert_called_once_with("A", "flight", "[]")

    def test_unknown_action_is_error(self):
        with patch.dict(os.environ, {"CORPUS_ACTION": "wipe"}, clear=True):
            self.assertEqual(corpus_action.main(), 2)


if __name__ == "__main__":
    unittest.main()
