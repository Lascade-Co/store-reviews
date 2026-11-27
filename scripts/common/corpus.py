"""Curated corpus of human review→reply pairs — the RAG knowledge base.

Two layers, both on the private R2 bucket:

  * a LanceDB table (vector + text + metadata) — the retrieval corpus;
  * append-only "pending" files, ``{R2_CORPUS_PREFIX}/pending/<key>.json`` — one
    per human reply, dropped by the reply workflow and folded into the table by
    the single consolidation job (``consolidate_corpus.py``).

Concurrency: the table is the only shared mutable object, and ONLY the
consolidation job writes it (matrix sync jobs just read). Each pending file has a
unique name (``{appcode}__{store}__{reviewId}``), so parallel reply runs never
collide. That is the whole write-safety design — see review.md.

LanceDB / pyarrow are imported lazily inside the table helpers so modules that
only touch pending files (post_reply.py) don't need those libraries installed.
"""

import json
import logging
import os

from common import publish


LOG = logging.getLogger(__name__)

TABLE_NAME = "replies"

# SQL filters for the three rating bands (see rating_band). Kept as rating ranges
# so the table needs no extra band column.
_BAND_FILTERS = {"neg": "rating <= 2", "mid": "rating = 3", "pos": "rating >= 4"}

_db = None
_table = None


# --------------------------------------------------------------------------- #
# Rating bands
# --------------------------------------------------------------------------- #
def rating_band(rating: object) -> str:
    """Map a 1–5 rating to a retrieval band: 1–2 neg, 3 mid, 4–5 pos."""
    try:
        value = int(rating)
    except (TypeError, ValueError):
        value = 0
    if value >= 4:
        return "pos"
    if value == 3:
        return "mid"
    return "neg"


# --------------------------------------------------------------------------- #
# R2 / LanceDB wiring
# --------------------------------------------------------------------------- #
def _corpus_prefix() -> str:
    return os.environ.get("R2_CORPUS_PREFIX", "corpus").strip("/")


def _storage_options() -> dict:
    account_id = publish._require_env("R2_ACCOUNT_ID")
    return {
        "endpoint": f"https://{account_id}.r2.cloudflarestorage.com",
        "aws_access_key_id": publish._require_env("R2_ACCESS_KEY_ID"),
        "aws_secret_access_key": publish._require_env("R2_SECRET_ACCESS_KEY"),
        "region": os.environ.get("R2_REGION", "auto").strip() or "auto",
    }


def _db_uri() -> str:
    return f"s3://{publish._require_env('R2_BUCKET')}/{_corpus_prefix()}/lancedb"


def _connect():
    global _db
    if _db is None:
        import lancedb

        _db = lancedb.connect(_db_uri(), storage_options=_storage_options())
    return _db


def _schema(dim: int):
    import pyarrow as pa

    return pa.schema(
        [
            pa.field("key", pa.string()),
            pa.field("vector", pa.list_(pa.float32(), dim)),
            pa.field("review", pa.string()),
            pa.field("reply", pa.string()),
            pa.field("app", pa.string()),
            pa.field("type", pa.string()),
            pa.field("store", pa.string()),
            pa.field("rating", pa.int32()),
            pa.field("lang", pa.string()),
            pa.field("created_at", pa.string()),
            pa.field("model", pa.string()),
        ]
    )


def _get_table(create_dim: int | None = None):
    """Open the corpus table, creating it (when ``create_dim`` is given) if absent.

    Returns None when the table does not exist yet and no dimension was supplied
    (cold start) so readers degrade gracefully to "no examples".
    """
    global _table
    if _table is not None:
        return _table
    db = _connect()
    if TABLE_NAME in set(db.table_names()):
        _table = db.open_table(TABLE_NAME)
    elif create_dim is not None:
        _table = db.create_table(TABLE_NAME, schema=_schema(create_dim))
    else:
        return None
    return _table


def _escape(value: str) -> str:
    return str(value).replace("'", "''")


# --------------------------------------------------------------------------- #
# Table operations (consolidation reads/writes, retrieval reads)
# --------------------------------------------------------------------------- #
def has_corpus() -> bool:
    """True when the table exists and holds at least one row.

    Lets retrieval short-circuit during the seeding period — no point loading the
    embedding model to query an empty corpus.
    """
    try:
        table = _get_table()
        return table is not None and table.count_rows() > 0
    except Exception:
        LOG.warning("Could not check corpus availability; treating as empty", exc_info=True)
        return False


def search(vector, *, type, rating_band, app=None, limit=20) -> list[dict]:
    """Nearest human replies in the same band+type, preferring the same app.

    Returns rows with an added ``similarity`` (cosine, 1.0 = identical). Empty
    when the corpus does not exist yet.
    """
    table = _get_table()
    if table is None:
        return []
    band_filter = _BAND_FILTERS.get(rating_band, _BAND_FILTERS["neg"])

    def _run(where: str) -> list[dict]:
        rows = (
            table.search(vector).metric("cosine").where(where, prefilter=True).limit(limit).to_list()
        )
        for row in rows:
            row["similarity"] = 1.0 - float(row.get("_distance", 1.0))
        return rows

    rows: list[dict] = []
    if app:
        rows = _run(f"app = '{_escape(app)}' AND type = '{_escape(type)}' AND {band_filter}")
    if not rows:
        rows = _run(f"type = '{_escape(type)}' AND {band_filter}")
    return rows


def upsert(row: dict) -> None:
    """Insert or replace a corpus row by ``key`` (idempotent)."""
    table = _get_table(create_dim=len(row["vector"]))
    (
        table.merge_insert("key")
        .when_matched_update_all()
        .when_not_matched_insert_all()
        .execute([row])
    )


def clear() -> None:
    """Drop the whole corpus table (removes every row).

    Used by the dashboard "clear" action to reset the corpus. The next ``upsert``
    recreates the table from the row's vector dimension, so a cleared corpus
    simply behaves like a cold start ("no examples") until fed again. No-op when
    the table does not exist yet.
    """
    global _table
    db = _connect()
    if TABLE_NAME in set(db.table_names()):
        db.drop_table(TABLE_NAME)
        LOG.info("Cleared corpus: dropped table %s", TABLE_NAME)
    else:
        LOG.info("Clear corpus: table %s does not exist yet — nothing to drop", TABLE_NAME)
    _table = None


# --------------------------------------------------------------------------- #
# Pending files (reply workflow writes, consolidation drains)
# --------------------------------------------------------------------------- #
def _pending_prefix() -> str:
    return f"{_corpus_prefix()}/pending/"


def _pending_key(key: str) -> str:
    return f"{_pending_prefix()}{key}.json"


def write_pending(key: str, obj: dict) -> None:
    """Drop one pending human reply. Unique key => no overwrite race."""
    client = publish._r2_client()
    client.put_object(
        Bucket=publish._require_env("R2_BUCKET"),
        Key=_pending_key(key),
        Body=json.dumps(obj, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )


def list_pending() -> list[tuple[str, dict]]:
    """Return ``(object_key, obj)`` for every pending file (object_key is the R2
    key, passed straight back to ``delete_pending``)."""
    client = publish._r2_client()
    bucket = publish._require_env("R2_BUCKET")
    prefix = _pending_prefix()
    out: list[tuple[str, dict]] = []
    token = None
    while True:
        kwargs = {"Bucket": bucket, "Prefix": prefix}
        if token:
            kwargs["ContinuationToken"] = token
        response = client.list_objects_v2(**kwargs)
        for item in response.get("Contents", []):
            object_key = item.get("Key", "")
            if not object_key.endswith(".json"):
                continue
            try:
                body = client.get_object(Bucket=bucket, Key=object_key)["Body"].read().decode("utf-8")
                obj = json.loads(body)
            except Exception:
                LOG.warning("Pending corpus file %s unreadable; skipping this run", object_key, exc_info=True)
                continue
            if isinstance(obj, dict):
                out.append((object_key, obj))
        if response.get("IsTruncated"):
            token = response.get("NextContinuationToken")
        else:
            break
    return out


def delete_pending(object_key: str) -> None:
    client = publish._r2_client()
    client.delete_object(Bucket=publish._require_env("R2_BUCKET"), Key=object_key)
