"""Retrieval-augmented generation for store-review replies (R2, no vector DB).

RAG improves reply quality by showing the model real human replies to SIMILAR
past reviews. The flow, all on CPU inside the existing GitHub Actions run:

  capture   a human reply is sent -> record_reply() stores the (review, reply)
            pair in R2 (text only; no model needed).
  refresh   the daily sync calls refresh_index() once per app: it embeds any
            newly-captured pairs and rewrites the consolidated corpus index.
  retrieve  during generation, attach_examples() embeds each new review, finds
            the top-k most similar past pairs by cosine similarity, and attaches
            their human replies to the Codex prompt as style references.

Everything is OPTIONAL and failure-safe, exactly like the Codex suggestion
layer: if the embedding library is missing, the model can't load, or R2 is
unreachable, every function degrades to a no-op and the sync/reply runs on
without examples. It never raises into its callers.

Embeddings use a multilingual sentence-transformers model (default
``intfloat/multilingual-e5-base``) so reviews in any language map into one
vector space; a Spanish review can retrieve a semantically similar Spanish — or
English — example. Vectors are L2-normalized, so cosine similarity is a dot
product. Switch models with RAG_EMBED_MODEL (re-embeds the corpus on next
refresh); turn the whole feature off with RAG_ENABLED=false.
"""

import logging
import os

from common import corpus
from common.state_manager import now_iso

try:  # numpy ships with sentence-transformers; keep the module importable without it
    import numpy as np
except Exception:  # pragma: no cover - only hit in a stripped env
    np = None


LOG = logging.getLogger(__name__)

DEFAULT_EMBED_MODEL = "intfloat/multilingual-e5-base"
DEFAULT_TOP_K = 3
DEFAULT_MIN_SIMILARITY = 0.80

# Examples are prompt context, not stored content, so clip them to keep the
# Codex prompt bounded no matter how long a reviewed/replied text was.
EXAMPLE_REVIEW_MAX_CHARS = 280
EXAMPLE_REPLY_MAX_CHARS = 340
# Reviews with no real text retrieve nothing useful; skip them.
_EMPTY_QUERIES = {"", "no review text provided."}

# Sentinel so a failed model load is remembered and never retried in-process.
_UNSET = object()
_MODEL = _UNSET
# Per-app in-memory cache of (model_name, item list, embedding matrix) so a run
# parses the index once even though both providers call attach_examples.
_INDEX_CACHE: dict[str, dict] = {}


def _enabled() -> bool:
    """RAG is on unless explicitly disabled (and numpy is importable)."""
    if np is None:
        return False
    return os.environ.get("RAG_ENABLED", "true").strip().lower() not in {"false", "0", "no", "off"}


def _model_name() -> str:
    return os.environ.get("RAG_EMBED_MODEL", "").strip() or DEFAULT_EMBED_MODEL


def _top_k() -> int:
    try:
        return max(1, int(os.environ.get("RAG_TOP_K", "").strip() or DEFAULT_TOP_K))
    except ValueError:
        return DEFAULT_TOP_K


def _min_similarity() -> float:
    try:
        return float(os.environ.get("RAG_MIN_SIMILARITY", "").strip() or DEFAULT_MIN_SIMILARITY)
    except ValueError:
        return DEFAULT_MIN_SIMILARITY


def _is_e5(model_name: str) -> bool:
    return "e5" in model_name.lower()


def _get_model():
    """Lazily load the embedding model once. Returns None if unavailable.

    sentence-transformers (and torch) are imported here, not at module load, so a
    run that needs no embedding never pays the import cost.
    """
    global _MODEL
    if _MODEL is not _UNSET:
        return _MODEL
    if not _enabled():
        _MODEL = None
        return None
    try:
        from sentence_transformers import SentenceTransformer

        name = _model_name()
        LOG.info("RAG: loading embedding model %s", name)
        _MODEL = SentenceTransformer(name)
    except Exception:
        LOG.warning("RAG: embedding model unavailable; proceeding without examples", exc_info=True)
        _MODEL = None
    return _MODEL


def _embed(texts: list[str], kind: str):
    """Embed texts (kind='query' or 'passage'); returns an (N, d) array or None.

    e5 models expect an ``query:`` / ``passage:`` instruction prefix; other
    models get the text unchanged.
    """
    model = _get_model()
    if model is None or not texts:
        return None
    prepared = texts
    if _is_e5(_model_name()):
        tag = "query: " if kind == "query" else "passage: "
        prepared = [tag + (t or "") for t in texts]
    try:
        return model.encode(prepared, normalize_embeddings=True, convert_to_numpy=True)
    except Exception:
        LOG.warning("RAG: embedding failed; proceeding without examples", exc_info=True)
        return None


def review_text_of(platform_rating_title_body: dict) -> str:
    """Join a review's title + body the way the corpus and queries both use."""
    title = (platform_rating_title_body.get("title") or "").strip()
    body = (platform_rating_title_body.get("body") or "").strip()
    return (f"{title}\n{body}" if title else body).strip()


def _clip(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[:limit].rstrip() + "…"


# --------------------------------------------------------------------------- #
# Capture
# --------------------------------------------------------------------------- #
def record_reply(
    app_code: str,
    platform: str,
    review_id: str,
    review_text: str,
    reply_text: str,
    rating=None,
    source: str = "dashboard",
) -> None:
    """Best-effort: store a human (review -> reply) pair for future retrieval.

    Never raises — a capture failure must not fail the reply that already landed
    at the store.
    """
    if not _enabled():
        return
    review_text = (review_text or "").strip()
    reply_text = (reply_text or "").strip()
    if not app_code or not platform or not review_id or not review_text or not reply_text:
        return
    if review_text.lower() in _EMPTY_QUERIES:
        return  # rating-only / placeholder review — nothing to match on later
    try:
        corpus.write_pair(
            app_code,
            {
                "appcode": app_code,
                "platform": platform,
                "review_id": review_id,
                "rating": rating,
                "review_text": review_text,
                "reply_text": reply_text,
                "source": source,
                "created_at": now_iso(),
            },
        )
    except Exception:
        LOG.warning("RAG: failed to record reply pair for %s/%s", platform, review_id, exc_info=True)


# --------------------------------------------------------------------------- #
# Index refresh (daily sync, single writer per app)
# --------------------------------------------------------------------------- #
def refresh_index(app_code: str) -> None:
    """Embed any newly-captured pairs and rewrite the app's corpus index.

    Cheap when nothing changed (no new pairs -> the model never loads). Never
    raises into the sync.
    """
    if not _enabled() or not app_code:
        return
    try:
        index = corpus.load_index(app_code) or {}
        current_model = _model_name()
        indexed_model = index.get("embed_model")
        items = index.get("items") if isinstance(index.get("items"), list) else []

        # A model change invalidates every stored vector — re-embed from scratch.
        model_changed = bool(items) and indexed_model != current_model
        existing = (
            {}
            if model_changed
            else {it.get("key"): it for it in items if isinstance(it, dict)}
        )

        kept: list[dict] = []        # unchanged items reused as-is (keep their vector)
        pending_meta: list[dict] = []  # new or changed pairs needing (re)embedding
        pending_text: list[str] = []
        seen_keys: set = set()
        for pair in corpus.iter_pairs(app_code):
            platform = pair.get("platform", "")
            review_id = pair.get("review_id", "")
            key = corpus.pair_hash(platform, review_id)
            if key in seen_keys:
                continue  # duplicate pair object; first one wins
            seen_keys.add(key)
            review_text = (pair.get("review_text") or "").strip()
            reply_text = (pair.get("reply_text") or "").strip()
            if not review_text or not reply_text:
                continue
            prev = existing.get(key)
            # Reuse the stored vector only when the text AND reply are unchanged;
            # an edited reply (same review_id, re-captured) must be re-embedded so
            # the index never serves a stale reply.
            if (
                isinstance(prev, dict)
                and prev.get("review_text") == review_text
                and prev.get("reply_text") == reply_text
                and isinstance(prev.get("embedding"), list)
            ):
                kept.append(prev)
                continue
            pending_meta.append(
                {
                    "key": key,
                    "platform": platform,
                    "review_id": review_id,
                    "rating": pair.get("rating"),
                    "review_text": review_text,
                    "reply_text": reply_text,
                }
            )
            pending_text.append(review_text)

        # Changed when anything is new/edited, when items were dropped (a pair
        # object deleted), or when the model changed.
        changed = bool(pending_meta) or len(kept) != len(items) or model_changed
        if not changed:
            LOG.info("RAG: corpus index for %s already current (%d item(s))", app_code, len(kept))
            _cache_index(app_code, current_model, kept)
            return

        if pending_text:
            vectors = _embed(pending_text, kind="passage")
            if vectors is None:
                return  # model unavailable; leave the index untouched
            for meta, vector in zip(pending_meta, vectors):
                meta["embedding"] = [float(x) for x in vector]
                kept.append(meta)

        new_index = {"embed_model": current_model, "items": kept}
        corpus.save_index(app_code, new_index)
        _cache_index(app_code, current_model, kept)
    except Exception:
        LOG.warning("RAG: index refresh failed for %s; continuing without it", app_code, exc_info=True)


def _cache_index(app_code: str, model_name: str, items: list[dict]) -> None:
    matrix = None
    if items and np is not None:
        try:
            matrix = np.array([it["embedding"] for it in items], dtype="float32")
        except Exception:
            matrix = None
    _INDEX_CACHE[app_code] = {"model": model_name, "items": items, "matrix": matrix}


def _ensure_cache(app_code: str) -> dict | None:
    if app_code in _INDEX_CACHE:
        return _INDEX_CACHE[app_code]
    index = corpus.load_index(app_code)
    if not isinstance(index, dict):
        _INDEX_CACHE[app_code] = {"model": _model_name(), "items": [], "matrix": None}
        return _INDEX_CACHE[app_code]
    items = index.get("items") if isinstance(index.get("items"), list) else []
    _cache_index(app_code, index.get("embed_model") or _model_name(), items)
    return _INDEX_CACHE[app_code]


# --------------------------------------------------------------------------- #
# Retrieval (generation)
# --------------------------------------------------------------------------- #
def _select_examples(items, matrix, query_vec, top_k, min_sim, exclude_review_id=None, query_text=None):
    """Pure top-k cosine selection over a normalized embedding matrix.

    ``matrix`` rows and ``query_vec`` are unit vectors, so cosine == dot product.
    Returns a list of clipped {"review", "reply"} dicts, most similar first,
    skipping the review's own past reply, any example identical to the review
    being answered (verbatim-copy risk), and duplicate pairs.
    """
    if matrix is None or query_vec is None or not len(items):
        return []
    query_norm = (query_text or "").strip().lower()
    sims = matrix.dot(query_vec)
    order = list(np.argsort(sims)[::-1])
    chosen = []
    seen: set = set()
    for idx in order:
        if len(chosen) >= top_k:
            break
        if float(sims[idx]) < min_sim:
            break  # sorted descending: nothing further can qualify
        item = items[idx]
        if exclude_review_id is not None and item.get("review_id") == exclude_review_id:
            continue  # never show a review its own past reply
        review = (item.get("review_text") or "").strip()
        reply = (item.get("reply_text") or "").strip()
        if not review or not reply:
            continue
        if query_norm and review.lower() == query_norm:
            continue  # identical review — would invite a verbatim copy
        dedup = (review.lower(), reply.lower())
        if dedup in seen:
            continue
        seen.add(dedup)
        chosen.append({"review": _clip(review, EXAMPLE_REVIEW_MAX_CHARS), "reply": _clip(reply, EXAMPLE_REPLY_MAX_CHARS)})
    return chosen


def attach_examples(app_code: str, items: list[dict]) -> list[dict]:
    """Attach an ``examples`` list of similar past (review, reply) pairs to each
    ai_reply payload item, in place. Returns ``items``. Never raises.

    ``items`` are the dicts ai_reply sends to Codex
    (``{id, platform, rating, title, body}``). An item gets an ``examples`` key
    only when at least one sufficiently-similar past reply exists.
    """
    if not _enabled() or not app_code or not items:
        return items
    try:
        cache = _ensure_cache(app_code)
        if not cache or not cache["items"] or cache["matrix"] is None:
            return items
        query_texts = [review_text_of(it) for it in items]
        query_vecs = _embed(query_texts, kind="query")
        if query_vecs is None:
            return items
        top_k, min_sim = _top_k(), _min_similarity()
        for item, qtext, qvec in zip(items, query_texts, query_vecs):
            if qtext.strip().lower() in _EMPTY_QUERIES:
                continue  # rating-only / empty review — nothing meaningful to match
            examples = _select_examples(
                cache["items"], cache["matrix"], qvec, top_k, min_sim,
                exclude_review_id=str(item.get("id") or ""),
                query_text=qtext,
            )
            if examples:
                item["examples"] = examples
    except Exception:
        LOG.warning("RAG: attaching examples failed; proceeding without them", exc_info=True)
    return items
