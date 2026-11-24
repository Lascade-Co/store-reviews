"""Retrieval gate for RAG few-shot examples.

For a new review, find the most similar PAST HUMAN replies to inject into the
generation prompt. The gate is deliberately strict: a reply is generated ONLY
when at least ``K`` examples clear the similarity ``THRESHOLD`` in the SAME
rating band and ``type`` (preferring the same app). Below that bar,
``retrieve_examples`` returns ``[]`` and the caller shows the review for a human
to answer from scratch — which seeds the corpus. This both avoids a cold-start
echo chamber and keeps generated replies anchored to real human precedent.

Retrieval is always optional: any failure logs and returns ``[]`` (generate/show
without examples), never blocking the sync.
"""

import logging
import os

from common import corpus, embeddings


LOG = logging.getLogger(__name__)


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


def _float_env(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default


# Tunable without a code change. THRESHOLD starts high (0.90) and is lowered
# using the per-candidate similarity scores logged below.
K = _int_env("RAG_MIN_EXAMPLES", 5)
THRESHOLD = _float_env("RAG_SIMILARITY_THRESHOLD", 0.90)


def retrieve_examples(
    review_text,
    *,
    app,
    type,
    rating,
    k=None,
    threshold=None,
    ready_fn=None,
    embed_fn=None,
    search_fn=None,
) -> list[dict]:
    """Return up to ``k`` ``{"review", "reply"}`` examples, or ``[]`` when the gate
    fails. ``ready_fn``/``embed_fn``/``search_fn`` are injectable for tests."""
    k = K if k is None else k
    threshold = THRESHOLD if threshold is None else threshold
    ready_fn = ready_fn or corpus.has_corpus
    embed_fn = embed_fn or embeddings.embed_texts
    search_fn = search_fn or corpus.search

    text = (review_text or "").strip()
    if not text:
        LOG.info("RAG retrieve: empty review text — skipping retrieval, review goes to manual")
        return []

    band = corpus.rating_band(rating)
    preview = text[:60].replace("\n", " ")
    LOG.info(
        "RAG retrieve: app=%s type=%s rating=%s band=%s k=%d threshold=%.2f review=%r",
        app, type, rating, band, k, threshold, preview,
    )

    try:
        if not ready_fn():
            # Empty corpus (seeding phase): don't load the model to query nothing.
            LOG.info("RAG retrieve: corpus empty — skipping retrieval (seeding phase), review goes to manual")
            return []
        vectors = embed_fn([text])
        if not vectors:
            LOG.info("RAG retrieve: embedding produced no vector — review goes to manual")
            return []
        rows = search_fn(vectors[0], type=type, rating_band=band, app=app, limit=k * 4)
    except Exception:
        LOG.warning("RAG retrieval failed; proceeding without examples", exc_info=True)
        return []

    scored = sorted(rows, key=lambda row: row.get("similarity", 0.0), reverse=True)
    LOG.info("RAG retrieve: %d candidate(s) in band=%s type=%s (app=%s)", len(scored), band, type, app)
    for row in scored:
        LOG.info("  RAG candidate key=%s similarity=%.4f", row.get("key"), row.get("similarity", 0.0))

    qualifying = [row for row in scored if row.get("similarity", 0.0) >= threshold]
    if len(qualifying) < k:
        LOG.info(
            "RAG gate CLOSED: %d/%d examples >= %.2f (app=%s type=%s band=%s) — review goes to manual",
            len(qualifying), k, threshold, app, type, band,
        )
        return []
    LOG.info(
        "RAG gate OPEN: %d qualifying (>= %.2f), using top %d as examples (app=%s type=%s band=%s)",
        len(qualifying), threshold, k, app, type, band,
    )
    return [{"review": row.get("review", ""), "reply": row.get("reply", "")} for row in qualifying[:k]]
