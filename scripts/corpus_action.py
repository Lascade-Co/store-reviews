"""Workflow entry for the dashboard's corpus actions: feed and clear.

Dispatched by reply-review.yml when the dashboard Feed / Clear button fires — the
workflow's ``action`` input routes to the matching job, which runs this script
with ``CORPUS_ACTION`` set. Inputs arrive as environment variables:

  CORPUS_ACTION = "feed" | "clear"
  APP_CODE      = current app's appcode (feed rows are tagged with it)
  APP_TYPE      = app category from apps.json (default type for fed rows)
  PAYLOAD       = feed mode: JSON array (or a single object) of
                  {review, reply, rating, [type], [lang], [store], [key]}

``feed`` embeds each review and upserts it into the LanceDB corpus immediately —
the same write the consolidation job does for a captured human reply, so it is
how examples (real seeds or test data) enter the corpus without waiting for the
capture loop. ``clear`` drops the whole corpus table. Both run in a job that
shares the ``corpus-consolidate`` concurrency group, so they never write the
table while consolidation is writing it.
"""

import hashlib
import json
import logging
import os
from datetime import datetime, timezone

from common import corpus, embeddings
from consolidate_corpus import build_row


LOG = logging.getLogger(__name__)

# feed rows are not tied to a store review; mark them so they are easy to spot.
# ``store`` is metadata only — retrieval filters on type+rating, not store — so
# this value never affects which examples are returned.
_FEED_STORE_DEFAULT = "manual"


def _feed_key(review: str, type_: str) -> str:
    """Stable key for a fed pair: re-feeding the same review updates, not dupes."""
    digest = hashlib.sha1(review.encode("utf-8")).hexdigest()[:12]
    return f"feed__{type_}__{digest}"


def feed_records(records, *, embed_fn=embeddings.embed_texts, upsert_fn=corpus.upsert) -> int:
    """Embed + upsert each ``{review, reply, app, type, rating, ...}`` record.

    Returns the number upserted. Raises ValueError on a record missing a required
    field so a malformed payload fails loudly rather than writing a useless row.
    """
    records = list(records)
    LOG.info("Feeding %d review->reply pair(s) into the corpus", len(records))
    count = 0
    for index, rec in enumerate(records):
        if not isinstance(rec, dict):
            raise ValueError(f"record {index} is not an object")
        review = (rec.get("review") or "").strip()
        reply = (rec.get("reply") or "").strip()
        app = (rec.get("app") or "").strip()
        type_ = (rec.get("type") or "").strip()
        if not (review and reply and app and type_):
            raise ValueError(f"record {index} needs non-empty review, reply, app, and type")
        obj = {
            "key": (rec.get("key") or "").strip() or _feed_key(review, type_),
            "review": review,
            "reply": reply,
            "app": app,
            "type": type_,
            "store": (rec.get("store") or "").strip() or _FEED_STORE_DEFAULT,
            "rating": int(rec.get("rating") or 0),
            "lang": (rec.get("lang") or "").strip(),
            "created_at": (rec.get("created_at") or "").strip()
            or datetime.now(timezone.utc).isoformat(),
        }
        vector = embed_fn([review])[0]
        upsert_fn(build_row(obj, vector))
        LOG.info("Fed %s (app=%s type=%s rating=%s store=%s)", obj["key"], app, type_, obj["rating"], obj["store"])
        count += 1
    LOG.info("Feed complete: %d pair(s) upserted into the corpus", count)
    return count


def parse_payload(raw: str) -> list[dict]:
    """Parse the feed PAYLOAD env into a list of record dicts.

    Tolerates a single object as well as an array. Raises on anything else.
    """
    data = json.loads(raw)
    if isinstance(data, dict):
        data = [data]  # a single pair is allowed
    if not isinstance(data, list):
        raise ValueError("feed PAYLOAD must be a JSON array (or a single object)")
    return data


def run_feed(app: str, app_type: str, raw_payload: str) -> int:
    """Stamp the current app/type onto each record and feed them. Returns rc."""
    if not app:
        LOG.error("APP_CODE is required for feed")
        return 1
    if not (raw_payload or "").strip():
        LOG.error("PAYLOAD is required for feed")
        return 1
    try:
        records = parse_payload(raw_payload)
    except (ValueError, json.JSONDecodeError) as exc:
        LOG.error("feed PAYLOAD is not valid JSON: %s", exc)
        return 1
    # The dashboard is scoped to one app, so stamp its app/type onto each record
    # (a record may still carry its own type for cross-category seeding).
    for rec in records:
        if isinstance(rec, dict):
            rec.setdefault("app", app)
            if not (rec.get("type") or "").strip():
                rec["type"] = app_type
    LOG.info("Feed request: app=%s type=%s, %d record(s)", app, app_type, len(records))
    try:
        feed_records(records)
    except ValueError as exc:
        LOG.error("feed rejected: %s", exc)
        return 1
    return 0


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    action = os.environ.get("CORPUS_ACTION", "").strip().lower()
    LOG.info("Corpus action: %s", action or "(none)")
    if action == "clear":
        corpus.clear()
        return 0
    if action == "feed":
        return run_feed(
            os.environ.get("APP_CODE", "").strip(),
            os.environ.get("APP_TYPE", "").strip(),
            os.environ.get("PAYLOAD", ""),
        )
    LOG.error("CORPUS_ACTION must be 'feed' or 'clear', got %r", action)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
