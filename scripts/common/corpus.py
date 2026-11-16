"""R2-backed corpus of human review replies for RAG (no vector DB).

The corpus teaches the reply model your team's voice by example. It lives in the
same R2 bucket as the dashboard data files, under two kinds of object:

  <R2_DATA_PREFIX>/corpus/<appcode>/pairs/<hash>.json
      One captured ``(review -> human reply)`` pair. Append-only and keyed by a
      hash of (platform, review_id), so two concurrent reply runs never clobber
      each other and re-writing the same pair is idempotent. No embedding is
      stored here — only the text — so the reply workflow needs no model to
      capture a pair.

  <R2_DATA_PREFIX>/corpus/<appcode>/index.json
      The consolidated retrieval index: every pair plus its precomputed
      embedding. Rebuilt by the single-writer sync job (which owns the embedding
      model); generation reads just this one object. See common/rag.py.

Only this module knows the R2 layout; rag.py owns the embedding/retrieval logic
and all failure handling. Functions here raise on hard R2 errors and let callers
decide — rag.py wraps every call so a corpus problem never breaks a run.
"""

import hashlib
import json
import logging

from common import publish


LOG = logging.getLogger(__name__)


def _prefix() -> str:
    return publish._require_env("R2_DATA_PREFIX").strip("/")


def _bucket() -> str:
    return publish._require_env("R2_BUCKET")


def pair_hash(platform: str, review_id: str) -> str:
    """Stable, key-safe id for a pair (review ids can contain '/' or ':')."""
    return hashlib.sha1(f"{platform}:{review_id}".encode("utf-8")).hexdigest()


def pairs_prefix(app_code: str) -> str:
    return f"{_prefix()}/corpus/{app_code}/pairs/"


def pair_key(app_code: str, platform: str, review_id: str) -> str:
    return f"{pairs_prefix(app_code)}{pair_hash(platform, review_id)}.json"


def index_key(app_code: str) -> str:
    return f"{_prefix()}/corpus/{app_code}/index.json"


def write_pair(app_code: str, pair: dict) -> None:
    """Store one (review, reply) pair as its own append-only object."""
    client = publish._r2_client()
    key = pair_key(app_code, pair["platform"], pair["review_id"])
    client.put_object(
        Bucket=_bucket(),
        Key=key,
        Body=json.dumps(pair, ensure_ascii=False, indent=2).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
    )
    LOG.info("Corpus: stored pair %s for %s", key, app_code)


def iter_pairs(app_code: str):
    """Yield every stored pair dict for an app (one R2 GET per pair)."""
    client = publish._r2_client()
    bucket = _bucket()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=pairs_prefix(app_code)):
        for obj in page.get("Contents", []) or []:
            key = obj.get("Key")
            if not key or not key.endswith(".json"):
                continue
            body = client.get_object(Bucket=bucket, Key=key)["Body"].read().decode("utf-8")
            try:
                data = json.loads(body)
            except json.JSONDecodeError:
                LOG.warning("Corpus: skipping unparseable pair %s", key)
                continue
            if isinstance(data, dict):
                yield data


def load_index(app_code: str) -> dict | None:
    """Return the consolidated index object, or None when absent/invalid."""
    client = publish._r2_client()
    try:
        response = client.get_object(Bucket=_bucket(), Key=index_key(app_code))
    except client.exceptions.NoSuchKey:
        return None
    except Exception as exc:  # missing perms, first run, etc.
        LOG.warning("Corpus: could not load index for %s: %s", app_code, exc)
        return None
    try:
        data = json.loads(response["Body"].read().decode("utf-8"))
    except json.JSONDecodeError:
        LOG.warning("Corpus: index for %s is not valid JSON; treating as empty", app_code)
        return None
    return data if isinstance(data, dict) else None


def save_index(app_code: str, index: dict) -> None:
    client = publish._r2_client()
    client.put_object(
        Bucket=_bucket(),
        Key=index_key(app_code),
        Body=json.dumps(index, ensure_ascii=False).encode("utf-8"),
        ContentType="application/json; charset=utf-8",
        CacheControl="no-cache",
    )
    LOG.info("Corpus: saved index for %s (%d item(s))", app_code, len(index.get("items", [])))
