import unittest
from unittest.mock import patch

from common import rag

try:
    import numpy as np
except Exception:  # pragma: no cover
    np = None


def _unit(vec):
    arr = np.array(vec, dtype="float32")
    return arr / (np.linalg.norm(arr) or 1.0)


@unittest.skipIf(np is None, "numpy not installed")
class SelectExamplesTests(unittest.TestCase):
    def setUp(self):
        self.items = [
            {"review_id": "a", "review_text": "love it", "reply_text": "Thanks!"},
            {"review_id": "b", "review_text": "refund please", "reply_text": "Contact the provider."},
            {"review_id": "c", "review_text": "great app", "reply_text": "Glad you like it."},
        ]
        self.matrix = np.array([_unit(v) for v in ([1, 0], [0, 1], [0.9, 0.1])], dtype="float32")

    def test_returns_top_k_by_similarity(self):
        out = rag._select_examples(self.items, self.matrix, _unit([1, 0]), top_k=2, min_sim=0.0)
        self.assertEqual([e["reply"] for e in out], ["Thanks!", "Glad you like it."])

    def test_min_similarity_filters(self):
        out = rag._select_examples(self.items, self.matrix, _unit([0, 1]), top_k=3, min_sim=0.95)
        self.assertEqual([e["reply"] for e in out], ["Contact the provider."])

    def test_excludes_same_review(self):
        out = rag._select_examples(self.items, self.matrix, _unit([1, 0]), top_k=3, min_sim=0.0, exclude_review_id="a")
        self.assertNotIn("Thanks!", [e["reply"] for e in out])


class CaptureTests(unittest.TestCase):
    def setUp(self):
        rag._MODEL = rag._UNSET
        rag._INDEX_CACHE.clear()

    @patch.dict("os.environ", {"RAG_ENABLED": "true"}, clear=False)
    @patch("common.corpus.write_pair")
    def test_record_reply_writes_pair(self, write_pair):
        rag.record_reply("A1", "appstore", "r1", "nice app", "Thank you!", rating=5)
        pair = write_pair.call_args.args[1]
        self.assertEqual(pair["review_id"], "r1")
        self.assertEqual(pair["reply_text"], "Thank you!")
        self.assertEqual(pair["source"], "dashboard")

    @patch.dict("os.environ", {"RAG_ENABLED": "false"}, clear=False)
    @patch("common.corpus.write_pair")
    def test_disabled_records_nothing(self, write_pair):
        rag.record_reply("A1", "appstore", "r1", "nice app", "Thank you!")
        write_pair.assert_not_called()

    @patch.dict("os.environ", {"RAG_ENABLED": "true"}, clear=False)
    @patch("common.corpus.write_pair")
    def test_missing_fields_skipped(self, write_pair):
        rag.record_reply("A1", "appstore", "r1", "", "reply")  # empty review text
        write_pair.assert_not_called()

    @patch.dict("os.environ", {"RAG_ENABLED": "true"}, clear=False)
    @patch("common.corpus.write_pair", side_effect=RuntimeError("r2 down"))
    def test_capture_never_raises(self, _write_pair):
        rag.record_reply("A1", "appstore", "r1", "nice", "reply")  # must not raise


@unittest.skipIf(np is None, "numpy not installed")
class AttachExamplesTests(unittest.TestCase):
    def setUp(self):
        rag._MODEL = rag._UNSET
        rag._INDEX_CACHE.clear()

    @patch.dict("os.environ", {"RAG_ENABLED": "true", "RAG_MIN_SIMILARITY": "0.0"}, clear=False)
    def test_attaches_examples_from_cache(self):
        rag._cache_index(
            "A1", "m",
            [{"review_id": "x", "review_text": "love", "reply_text": "Thanks!", "embedding": [1.0, 0.0]}],
        )
        items = [{"id": "r1", "platform": "appstore", "rating": 5, "title": "", "body": "love"}]
        with patch.object(rag, "_embed", return_value=np.array([_unit([1, 0])])):
            rag.attach_examples("A1", items)
        self.assertEqual(items[0]["examples"], [{"review": "love", "reply": "Thanks!"}])

    @patch.dict("os.environ", {"RAG_ENABLED": "false"}, clear=False)
    def test_disabled_attaches_nothing(self):
        items = [{"id": "r1", "platform": "appstore", "title": "", "body": "love"}]
        rag.attach_examples("A1", items)
        self.assertNotIn("examples", items[0])

    @patch.dict("os.environ", {"RAG_ENABLED": "true"}, clear=False)
    def test_empty_index_attaches_nothing(self):
        rag._cache_index("A1", "m", [])
        items = [{"id": "r1", "platform": "appstore", "title": "", "body": "love"}]
        rag.attach_examples("A1", items)
        self.assertNotIn("examples", items[0])


@unittest.skipIf(np is None, "numpy not installed")
class RefreshIndexTests(unittest.TestCase):
    def setUp(self):
        rag._MODEL = rag._UNSET
        rag._INDEX_CACHE.clear()

    @patch.dict("os.environ", {"RAG_ENABLED": "true", "RAG_EMBED_MODEL": "intfloat/multilingual-e5-base"}, clear=False)
    def test_embeds_new_pairs_and_saves(self):
        pairs = [{"platform": "appstore", "review_id": "r1", "review_text": "hi", "reply_text": "hello"}]
        with patch("common.corpus.load_index", return_value=None), \
             patch("common.corpus.iter_pairs", return_value=iter(pairs)), \
             patch("common.corpus.save_index") as save_index, \
             patch.object(rag, "_embed", return_value=np.array([[0.6, 0.8]], dtype="float32")):
            rag.refresh_index("A1")
        saved = save_index.call_args.args[1]
        self.assertEqual(saved["embed_model"], "intfloat/multilingual-e5-base")
        self.assertEqual(len(saved["items"]), 1)
        self.assertEqual(saved["items"][0]["review_id"], "r1")
        self.assertIn("embedding", saved["items"][0])

    @patch.dict("os.environ", {"RAG_ENABLED": "true", "RAG_EMBED_MODEL": "intfloat/multilingual-e5-base"}, clear=False)
    def test_no_new_pairs_does_not_resave(self):
        key = rag_corpus_hash("appstore", "r1")
        index = {"embed_model": "intfloat/multilingual-e5-base",
                 "items": [{"key": key, "platform": "appstore", "review_id": "r1",
                            "review_text": "hi", "reply_text": "hello", "embedding": [0.6, 0.8]}]}
        pairs = [{"platform": "appstore", "review_id": "r1", "review_text": "hi", "reply_text": "hello"}]
        with patch("common.corpus.load_index", return_value=index), \
             patch("common.corpus.iter_pairs", return_value=iter(pairs)), \
             patch("common.corpus.save_index") as save_index:
            rag.refresh_index("A1")
        save_index.assert_not_called()


def rag_corpus_hash(platform, review_id):
    from common import corpus
    return corpus.pair_hash(platform, review_id)


if __name__ == "__main__":
    unittest.main()
