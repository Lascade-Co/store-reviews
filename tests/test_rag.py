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

    def test_clips_long_example_text(self):
        items = [{"review_id": "z", "review_text": "x" * 400, "reply_text": "y" * 400}]
        matrix = np.array([_unit([1, 0])], dtype="float32")
        out = rag._select_examples(items, matrix, _unit([1, 0]), top_k=1, min_sim=0.0)
        self.assertTrue(out[0]["review"].endswith("…"))
        self.assertLessEqual(len(out[0]["review"]), rag.EXAMPLE_REVIEW_MAX_CHARS + 1)
        self.assertLessEqual(len(out[0]["reply"]), rag.EXAMPLE_REPLY_MAX_CHARS + 1)

    def test_excludes_example_identical_to_query(self):
        out = rag._select_examples(self.items, self.matrix, _unit([1, 0]), top_k=3, min_sim=0.0, query_text="love it")
        self.assertNotIn("love it", [e["review"] for e in out])

    def test_dedupes_identical_pairs(self):
        items = [
            {"review_id": "a", "review_text": "dup", "reply_text": "same"},
            {"review_id": "b", "review_text": "dup", "reply_text": "same"},
        ]
        matrix = np.array([_unit([1, 0]), _unit([0.9, 0.1])], dtype="float32")
        out = rag._select_examples(items, matrix, _unit([1, 0]), top_k=3, min_sim=0.0)
        self.assertEqual(len(out), 1)


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

    @patch.dict("os.environ", {"RAG_ENABLED": "true"}, clear=False)
    @patch("common.corpus.write_pair")
    def test_placeholder_review_not_captured(self, write_pair):
        rag.record_reply("A1", "appstore", "r1", "No review text provided.", "Thanks!")
        write_pair.assert_not_called()


@unittest.skipIf(np is None, "numpy not installed")
class AttachExamplesTests(unittest.TestCase):
    def setUp(self):
        rag._MODEL = rag._UNSET
        rag._INDEX_CACHE.clear()

    @patch.dict("os.environ", {"RAG_ENABLED": "true", "RAG_MIN_SIMILARITY": "0.0"}, clear=False)
    def test_attaches_examples_from_cache(self):
        rag._cache_index(
            "A1", "m",
            [{"review_id": "x", "review_text": "adore this app", "reply_text": "Thanks!", "embedding": [1.0, 0.0]}],
        )
        items = [{"id": "r1", "platform": "appstore", "rating": 5, "title": "", "body": "love this app"}]
        with patch.object(rag, "_embed", return_value=np.array([_unit([1, 0])])):
            rag.attach_examples("A1", items)
        self.assertEqual(items[0]["examples"], [{"review": "adore this app", "reply": "Thanks!"}])

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

    @patch.dict("os.environ", {"RAG_ENABLED": "true", "RAG_MIN_SIMILARITY": "0.0"}, clear=False)
    def test_empty_query_review_is_skipped(self):
        rag._cache_index(
            "A1", "m",
            [{"review_id": "x", "review_text": "love", "reply_text": "Thanks!", "embedding": [1.0, 0.0]}],
        )
        items = [{"id": "r1", "platform": "appstore", "rating": 5, "title": "", "body": ""}]
        with patch.object(rag, "_embed", return_value=np.array([_unit([1, 0])])):
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
    def test_changed_reply_is_reembedded(self):
        key = rag_corpus_hash("appstore", "r1")
        index = {"embed_model": "intfloat/multilingual-e5-base",
                 "items": [{"key": key, "platform": "appstore", "review_id": "r1",
                            "review_text": "hi", "reply_text": "OLD", "embedding": [0.6, 0.8]}]}
        pairs = [{"platform": "appstore", "review_id": "r1", "review_text": "hi", "reply_text": "NEW"}]
        with patch("common.corpus.load_index", return_value=index), \
             patch("common.corpus.iter_pairs", return_value=iter(pairs)), \
             patch("common.corpus.save_index") as save_index, \
             patch.object(rag, "_embed", return_value=np.array([[0.0, 1.0]], dtype="float32")):
            rag.refresh_index("A1")
        saved = save_index.call_args.args[1]
        self.assertEqual(len(saved["items"]), 1)
        self.assertEqual(saved["items"][0]["reply_text"], "NEW")
        self.assertEqual(saved["items"][0]["embedding"], [0.0, 1.0])

    @patch.dict("os.environ", {"RAG_ENABLED": "true", "RAG_EMBED_MODEL": "intfloat/multilingual-e5-base"}, clear=False)
    def test_deleted_pair_drops_from_index(self):
        k1, k2 = rag_corpus_hash("appstore", "r1"), rag_corpus_hash("appstore", "r2")
        index = {"embed_model": "intfloat/multilingual-e5-base", "items": [
            {"key": k1, "platform": "appstore", "review_id": "r1", "review_text": "hi", "reply_text": "a", "embedding": [1.0, 0.0]},
            {"key": k2, "platform": "appstore", "review_id": "r2", "review_text": "bye", "reply_text": "b", "embedding": [0.0, 1.0]},
        ]}
        pairs = [{"platform": "appstore", "review_id": "r1", "review_text": "hi", "reply_text": "a"}]  # r2 gone
        with patch("common.corpus.load_index", return_value=index), \
             patch("common.corpus.iter_pairs", return_value=iter(pairs)), \
             patch("common.corpus.save_index") as save_index:
            rag.refresh_index("A1")
        saved = save_index.call_args.args[1]
        self.assertEqual([it["review_id"] for it in saved["items"]], ["r1"])

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
