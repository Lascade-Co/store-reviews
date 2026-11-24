import unittest
from unittest.mock import Mock

from common import rag


def _rows(*sims):
    """Fake corpus rows carrying a similarity each."""
    return [
        {"key": f"k{i}", "review": f"review {i}", "reply": f"reply {i}", "similarity": sim}
        for i, sim in enumerate(sims)
    ]


class RetrieveExamplesTests(unittest.TestCase):
    def _retrieve(self, rows, *, k=3, threshold=0.90, ready=True, embed=None):
        embed_fn = embed or Mock(return_value=[[0.0, 0.0]])
        search_fn = Mock(return_value=rows)
        result = rag.retrieve_examples(
            "lovely app",
            app="D1AS",
            type="flight",
            rating=5,
            k=k,
            threshold=threshold,
            ready_fn=lambda: ready,
            embed_fn=embed_fn,
            search_fn=search_fn,
        )
        return result, embed_fn, search_fn

    def test_gate_passes_with_enough_qualifying_examples(self):
        result, _, search_fn = self._retrieve(_rows(0.97, 0.95, 0.93, 0.80))
        self.assertEqual(result, [
            {"review": "review 0", "reply": "reply 0"},
            {"review": "review 1", "reply": "reply 1"},
            {"review": "review 2", "reply": "reply 2"},
        ])
        # Band for a 5-star review is "pos"; the search filter is honored upstream.
        self.assertEqual(search_fn.call_args.kwargs["rating_band"], "pos")

    def test_gate_closes_when_too_few_clear_threshold(self):
        # Only two of four beat 0.90 -> below K=3 -> no suggestion.
        result, _, _ = self._retrieve(_rows(0.97, 0.91, 0.5, 0.4))
        self.assertEqual(result, [])

    def test_returns_only_top_k_even_when_more_qualify(self):
        result, _, _ = self._retrieve(_rows(0.99, 0.98, 0.97, 0.96, 0.95), k=2)
        self.assertEqual([r["review"] for r in result], ["review 0", "review 1"])

    def test_empty_text_short_circuits_without_embedding(self):
        embed = Mock()
        result = rag.retrieve_examples(
            "   ", app="D1AS", type="flight", rating=5,
            ready_fn=lambda: True, embed_fn=embed, search_fn=Mock(return_value=[]),
        )
        self.assertEqual(result, [])
        embed.assert_not_called()

    def test_empty_corpus_skips_embedding(self):
        embed = Mock()
        result, embed_fn, search_fn = self._retrieve(_rows(0.99), ready=False, embed=embed)
        self.assertEqual(result, [])
        embed.assert_not_called()  # model never loaded to query an empty corpus
        search_fn.assert_not_called()

    def test_retrieval_failure_degrades_to_no_examples(self):
        result = rag.retrieve_examples(
            "lovely app", app="D1AS", type="flight", rating=5,
            ready_fn=lambda: True,
            embed_fn=Mock(side_effect=RuntimeError("model blew up")),
            search_fn=Mock(),
        )
        self.assertEqual(result, [])


if __name__ == "__main__":
    unittest.main()
