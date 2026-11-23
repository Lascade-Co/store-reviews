import unittest
from unittest.mock import Mock

from consolidate_corpus import consolidate


def _pending(**over):
    obj = {
        "key": "D1AS__playstore__r1",
        "review": "Loved the deals",
        "reply": "Thanks a lot!",
        "app": "D1AS",
        "type": "flight",
        "store": "playstore",
        "rating": 5,
        "lang": "en",
        "created_at": "2026-11-20T00:00:00+00:00",
    }
    obj.update(over)
    return obj


class ConsolidateTests(unittest.TestCase):
    def test_happy_path_upserts_then_deletes(self):
        embed = Mock(return_value=[[0.1, 0.2, 0.3]])
        upsert = Mock()
        delete = Mock()

        upserted, left = consolidate(
            [("corpus/pending/k1.json", _pending())],
            embed_fn=embed, upsert_fn=upsert, delete_pending_fn=delete,
        )

        self.assertEqual((upserted, left), (1, 0))
        row = upsert.call_args.args[0]
        self.assertEqual(row["key"], "D1AS__playstore__r1")
        self.assertEqual(row["vector"], [0.1, 0.2, 0.3])
        self.assertEqual(row["review"], "Loved the deals")
        self.assertIn("model", row)
        delete.assert_called_once_with("corpus/pending/k1.json")

    def test_upsert_failure_leaves_pending_for_retry(self):
        upsert = Mock(side_effect=RuntimeError("lancedb down"))
        delete = Mock()

        upserted, left = consolidate(
            [("corpus/pending/k1.json", _pending())],
            embed_fn=Mock(return_value=[[0.1]]), upsert_fn=upsert, delete_pending_fn=delete,
        )

        self.assertEqual((upserted, left), (0, 1))
        delete.assert_not_called()  # the file survives for the next run

    def test_unusable_file_is_discarded_not_upserted(self):
        upsert = Mock()
        delete = Mock()

        upserted, left = consolidate(
            [("corpus/pending/empty.json", _pending(review="   ", key="k"))],
            embed_fn=Mock(), upsert_fn=upsert, delete_pending_fn=delete,
        )

        self.assertEqual((upserted, left), (0, 0))
        upsert.assert_not_called()
        delete.assert_called_once_with("corpus/pending/empty.json")

    def test_one_bad_file_does_not_block_the_rest(self):
        upsert = Mock(side_effect=[RuntimeError("boom"), None])
        delete = Mock()

        upserted, left = consolidate(
            [
                ("corpus/pending/bad.json", _pending(key="bad")),
                ("corpus/pending/good.json", _pending(key="good")),
            ],
            embed_fn=Mock(return_value=[[0.1]]), upsert_fn=upsert, delete_pending_fn=delete,
        )

        self.assertEqual((upserted, left), (1, 1))
        delete.assert_called_once_with("corpus/pending/good.json")


if __name__ == "__main__":
    unittest.main()
