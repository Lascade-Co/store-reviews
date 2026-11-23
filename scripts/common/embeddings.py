"""Multilingual sentence embeddings via fastembed (ONNX, CPU).

The embedding MODEL — not the vector store — is what makes retrieval work across
languages: it maps a review in any language to a vector, and similar meanings
land near each other. BGE-M3 is multilingual, 1024-dim, and runs on CPU through
ONNX (no torch), which suits a short-lived GitHub Actions job.

The model is loaded lazily and cached at module level, so a batch of reviews
embeds with one instance. Weights are cached on disk (FASTEMBED_CACHE_PATH) so
the workflow restores them with actions/cache instead of re-downloading (the
model is large relative to the job timeout). MODEL_NAME is stored with every
corpus row so a model change is detectable — old and new vectors are
incompatible and the corpus would need re-embedding.
"""

import logging
import os


LOG = logging.getLogger(__name__)

# BGE-M3: multilingual, 1024-dim, 8192-token context, MIT-licensed. Override with
# EMBED_MODEL to fall back to a lighter multilingual model (e.g.
# "intfloat/multilingual-e5-base") if fastembed lacks BGE-M3 at build time.
MODEL_NAME = os.environ.get("EMBED_MODEL", "BAAI/bge-m3").strip() or "BAAI/bge-m3"

_model = None


def get_model():
    """Return the shared fastembed model, loading it on first use."""
    global _model
    if _model is None:
        # Imported lazily so importing this module (e.g. in the reply workflow,
        # which only writes pending files) never needs fastembed installed.
        from fastembed import TextEmbedding

        kwargs = {}
        cache_dir = os.environ.get("FASTEMBED_CACHE_PATH", "").strip()
        if cache_dir:
            kwargs["cache_dir"] = cache_dir
        LOG.info("Loading embedding model %s", MODEL_NAME)
        _model = TextEmbedding(model_name=MODEL_NAME, **kwargs)
    return _model


def embed_texts(texts: list[str]) -> list[list[float]]:
    """Embed a list of texts → one float vector per text (same order)."""
    if not texts:
        return []
    model = get_model()
    # fastembed .embed() yields numpy arrays; make them plain JSON-safe lists.
    return [[float(x) for x in vector] for vector in model.embed(list(texts))]
