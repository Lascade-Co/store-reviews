import io
import json
import os
import unittest
from unittest.mock import Mock, patch

from common import corpus


class RatingBandTests(unittest.TestCase):
    def test_bands(self):
        self.assertEqual(corpus.rating_band(1), "neg")
        self.assertEqual(corpus.rating_band(2), "neg")
        self.assertEqual(corpus.rating_band(3), "mid")
        self.assertEqual(corpus.rating_band(4), "pos")
        self.assertEqual(corpus.rating_band(5), "pos")

    def test_unparseable_rating_falls_back_to_neg(self):
        self.assertEqual(corpus.rating_band(None), "neg")
        self.assertEqual(corpus.rating_band("x"), "neg")


class ClearTests(unittest.TestCase):
    def setUp(self):
        # clear() caches the table at module level; reset it around each test.
        self._saved_table = corpus._table
        corpus._table = Mock()
        self.addCleanup(lambda: setattr(corpus, "_table", self._saved_table))

    def test_drops_table_when_present_and_resets_cache(self):
        db = Mock()
        db.table_names.return_value = [corpus.TABLE_NAME, "other"]
        with patch("common.corpus._connect", return_value=db):
            corpus.clear()
        db.drop_table.assert_called_once_with(corpus.TABLE_NAME)
        self.assertIsNone(corpus._table)  # cache cleared so next op reopens

    def test_no_op_when_table_absent(self):
        db = Mock()
        db.table_names.return_value = ["other"]
        with patch("common.corpus._connect", return_value=db):
            corpus.clear()
        db.drop_table.assert_not_called()
        self.assertIsNone(corpus._table)


class PendingFileTests(unittest.TestCase):
    def setUp(self):
        self.env = patch.dict(os.environ, {"R2_BUCKET": "b"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)
        self.client = Mock()
        self.client_patch = patch("common.publish._r2_client", return_value=self.client)
        self.client_patch.start()
        self.addCleanup(self.client_patch.stop)

    def test_write_pending_uses_namespaced_key(self):
        corpus.write_pending("D1AS__playstore__r1", {"key": "D1AS__playstore__r1", "review": "hi"})
        self.client.put_object.assert_called_once()
        kwargs = self.client.put_object.call_args.kwargs
        self.assertEqual(kwargs["Bucket"], "b")
        self.assertEqual(kwargs["Key"], "corpus/pending/D1AS__playstore__r1.json")
        self.assertEqual(json.loads(kwargs["Body"].decode("utf-8"))["review"], "hi")

    def test_list_pending_reads_and_parses_each_file(self):
        self.client.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "corpus/pending/k1.json"},
                {"Key": "corpus/pending/"},  # the folder marker, skipped (no .json)
            ],
            "IsTruncated": False,
        }
        body = io.BytesIO(json.dumps({"key": "k1", "review": "r"}).encode("utf-8"))
        self.client.get_object.return_value = {"Body": body}

        result = corpus.list_pending()

        self.assertEqual(result, [("corpus/pending/k1.json", {"key": "k1", "review": "r"})])

    def test_list_pending_skips_unreadable_file(self):
        self.client.list_objects_v2.return_value = {
            "Contents": [{"Key": "corpus/pending/bad.json"}],
            "IsTruncated": False,
        }
        self.client.get_object.return_value = {"Body": io.BytesIO(b"{not json")}

        self.assertEqual(corpus.list_pending(), [])

    def test_delete_pending_targets_the_object_key(self):
        corpus.delete_pending("corpus/pending/k1.json")
        self.client.delete_object.assert_called_once_with(Bucket="b", Key="corpus/pending/k1.json")


if __name__ == "__main__":
    unittest.main()
