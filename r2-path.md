# R2 storage layout

Every object lives in one bucket (`R2_BUCKET`). Prefixes come from environment
variables (defaults noted). Review/reply **text** only ever lives on R2 — git
holds hashes and ids only.

## Layout

```
s3://<R2_BUCKET>/
│
├── <R2_DATA_PREFIX>/                      ← dashboard review files (one per app)
│   ├── D1AS_D1IS.json                        Airlines70   — the pending-review list
│   ├── M4AS_M4IS.json                        Flight Deals    the dashboard reads &
│   ├── D1AF.json                             Flights70       the reply workflow updates
│   ├── D1AH.json                             Hotels70
│   └── D1AC.json                             Rent80
│
└── <R2_CORPUS_PREFIX>/   (default: "corpus")  ← the RAG corpus
    │
    ├── pending/                           ← human-reply capture (transient)
    │   ├── D1AS_D1IS__playstore__gp:AOqpTO….json
    │   ├── M4AS_M4IS__appstore__1234567890.json
    │   └── …                                 one file per human reply; written on
    │                                         send, deleted after consolidation
    │
    └── lancedb/                           ← the common vector DB (LanceDB dataset)
        └── replies.lance/                    the one "replies" table (all apps/types)
            ├── data/        *.lance             rows, columnar (key, vector, review,
            ├── _versions/                       reply, app, type, store, rating, …)
            └── _transactions/
```

## Path-by-path

| What | R2 path | Written by | Read by |
|---|---|---|---|
| **Dashboard review file** (per app) | `<R2_DATA_PREFIX>/<appcode>.json` | `publish.py` (sync run) + `post_reply.py` (on reply) | the dashboard; reply workflow |
| **Corpus pending** (per human reply) | `<R2_CORPUS_PREFIX>/pending/<appcode>__<store>__<reviewId>.json` | `post_reply._capture_corpus` → `corpus.write_pending` | `consolidate_corpus.py`, then **deleted** on success |
| **Common vector DB** (LanceDB) | `<R2_CORPUS_PREFIX>/lancedb/` → table `replies.lance/` | `consolidate_corpus.py` → `corpus.upsert` | `rag.retrieve_examples` → `corpus.search` |

The pending `<key>` is `{appcode}__{store}__{reviewId}` (store = `playstore` or
`appstore`).

## LanceDB `replies` table columns

| Column | Type | Notes |
|---|---|---|
| `key` | string | `{appcode}__{store}__{reviewId}` — unique, upsert key |
| `vector` | fixed_size_list<float32>[1024] | BGE-M3 embedding of the review |
| `review` | string | reviewer's text |
| `reply` | string | the human reply |
| `app` | string | appcode |
| `type` | string | `flight` \| `car` \| `hotel` — RAG filter |
| `store` | string | `playstore` \| `appstore` |
| `rating` | int32 | 1–5 |
| `lang` | string | reviewer language |
| `created_at` | string | ISO timestamp |
| `model` | string | embedding model id (e.g. `BAAI/bge-m3`) |

## Env vars

- `R2_BUCKET` — the bucket (all paths live here)
- `R2_DATA_PREFIX` — dashboard files prefix (required, no default)
- `R2_CORPUS_PREFIX` — corpus root (default `corpus`)
- `R2_ACCOUNT_ID`, `R2_ACCESS_KEY_ID`, `R2_SECRET_ACCESS_KEY`, `R2_REGION` (default `auto`)

## Not on R2

- **State files** (`posted_ids`, review map, baseline, reply hashes) →
  local filesystem → git, at `state/<appcode>/<provider>.json`. No review text,
  hashes/ids only — safe in git.
- **`apps.json`** → in the repo, not R2.
