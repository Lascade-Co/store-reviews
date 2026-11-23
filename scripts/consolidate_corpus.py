"""Fold pending human replies into the corpus — the ONLY LanceDB writer.

Runs once per sync as a single post-matrix job (see review-sync.yml). For each
pending file on R2 it embeds the review text, upserts the row into the LanceDB
table by key, and deletes the pending file ONLY after a successful upsert — so a
transient failure simply leaves the file for the next run (self-healing). One
bad file never aborts the rest.
"""

import logging

from common import corpus, embeddings


LOG = logging.getLogger(__name__)


def consolidate(pending, *, embed_fn, upsert_fn, delete_pending_fn) -> tuple[int, int]:
    """Embed → upsert → delete each pending entry. Returns (upserted, left)."""
    upserted = 0
    left = 0
    for object_key, obj in pending:
        try:
            review = (obj.get("review") or "").strip()
            key = obj.get("key")
            if not review or not key:
                # Unusable (no text or no primary key) — never embeddable; drop it
                # so it doesn't block the queue forever.
                LOG.warning("Pending file %s has no review/key; discarding as unusable", object_key)
                delete_pending_fn(object_key)
                continue
            vector = embed_fn([review])[0]
            row = {
                "key": key,
                "vector": vector,
                "review": review,
                "reply": (obj.get("reply") or "").strip(),
                "app": obj.get("app") or "",
                "type": obj.get("type") or "",
                "store": obj.get("store") or "",
                "rating": int(obj.get("rating") or 0),
                "lang": obj.get("lang") or "",
                "created_at": obj.get("created_at") or "",
                "model": embeddings.MODEL_NAME,
            }
            upsert_fn(row)
            delete_pending_fn(object_key)
            upserted += 1
        except Exception:
            LOG.warning("Could not consolidate %s; leaving it for the next run", object_key, exc_info=True)
            left += 1
    LOG.info("Corpus consolidation complete: %d upserted, %d left for retry", upserted, left)
    return upserted, left


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    pending = corpus.list_pending()
    if not pending:
        LOG.info("No pending corpus files; nothing to consolidate")
        return 0
    LOG.info("Consolidating %d pending corpus file(s)", len(pending))
    consolidate(
        pending,
        embed_fn=embeddings.embed_texts,
        upsert_fn=corpus.upsert,
        delete_pending_fn=corpus.delete_pending,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
