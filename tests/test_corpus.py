import json
import unittest
from unittest.mock import MagicMock, patch

from common import corpus


ENV = {"R2_DATA_PREFIX": "pfx", "R2_BUCKET": "bkt"}


class KeyTests(unittest.TestCase):
    def test_pair_hash_is_stable_and_key_safe(self):
        h = corpus.pair_hash("playstore", "gp:AOqp/with:weird")
        self.assertEqual(h, corpus.pair_hash("playstore", "gp:AOqp/with:weird"))
        self.assertNotIn("/", h)
        self.assertNotIn(":", h)
        self.assertNotEqual(h, corpus.pair_hash("appstore", "gp:AOqp/with:weird"))

    @patch.dict("os.environ", ENV, clear=False)
    def test_key_layout(self):
        self.assertEqual(corpus.pairs_prefix("A1"), "pfx/corpus/A1/pairs/")
        self.assertEqual(corpus.index_key("A1"), "pfx/corpus/A1/index.json")
        self.assertTrue(corpus.pair_key("A1", "appstore", "r1").startswith("pfx/corpus/A1/pairs/"))
        self.assertTrue(corpus.pair_key("A1", "appstore", "r1").endswith(".json"))


class IOTests(unittest.TestCase):
    def setUp(self):
        self.client = MagicMock()
        self.client.exceptions.NoSuchKey = type("NoSuchKey", (Exception,), {})
        patcher = patch("common.publish._r2_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict("os.environ", ENV, clear=False)
        env.start()
        self.addCleanup(env.stop)

    def test_write_pair_puts_expected_object(self):
        corpus.write_pair("A1", {"platform": "appstore", "review_id": "r1", "review_text": "t", "reply_text": "y"})
        kwargs = self.client.put_object.call_args.kwargs
        self.assertEqual(kwargs["Bucket"], "bkt")
        self.assertEqual(kwargs["Key"], corpus.pair_key("A1", "appstore", "r1"))
        self.assertEqual(json.loads(kwargs["Body"].decode("utf-8"))["review_id"], "r1")

    def test_iter_pairs_lists_and_gets(self):
        paginator = MagicMock()
        paginator.paginate.return_value = [
            {"Contents": [{"Key": "pfx/corpus/A1/pairs/a.json"}, {"Key": "pfx/corpus/A1/pairs/skip.txt"}]},
        ]
        self.client.get_paginator.return_value = paginator
        body = MagicMock()
        body.read.return_value = json.dumps({"review_id": "r1", "platform": "appstore"}).encode("utf-8")
        self.client.get_object.return_value = {"Body": body}

        pairs = list(corpus.iter_pairs("A1"))
        self.assertEqual(pairs, [{"review_id": "r1", "platform": "appstore"}])
        # only the .json object was fetched
        self.client.get_object.assert_called_once()

    def test_load_index_missing_returns_none(self):
        self.client.get_object.side_effect = self.client.exceptions.NoSuchKey()
        self.assertIsNone(corpus.load_index("A1"))

    def test_load_index_parses_object(self):
        body = MagicMock()
        body.read.return_value = json.dumps({"embed_model": "m", "items": []}).encode("utf-8")
        self.client.get_object.side_effect = None
        self.client.get_object.return_value = {"Body": body}
        self.assertEqual(corpus.load_index("A1"), {"embed_model": "m", "items": []})


if __name__ == "__main__":
    unittest.main()
