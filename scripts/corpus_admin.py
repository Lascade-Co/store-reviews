"""Local corpus inspection / cleanup CLI (not run by any workflow).

    python scripts/corpus_admin.py export [out.json]   # dump all rows (no vectors)
    python scripts/corpus_admin.py delete <key>        # remove one bad entry

Needs the R2 env vars (R2_ACCOUNT_ID / R2_ACCESS_KEY_ID / R2_SECRET_ACCESS_KEY /
R2_BUCKET) and lancedb+fastembed installed, same as the consolidation job.
"""

import json
import logging
import sys

from common import corpus


LOG = logging.getLogger(__name__)

USAGE = "usage: corpus_admin.py export [out.json] | delete <key>"


def main(argv: list[str]) -> int:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not argv or argv[0] not in {"export", "delete"}:
        print(USAGE)
        return 2

    command = argv[0]
    if command == "export":
        rows = corpus.export()
        text = json.dumps(rows, ensure_ascii=False, indent=2)
        out = argv[1] if len(argv) > 1 else None
        if out:
            with open(out, "w", encoding="utf-8") as handle:
                handle.write(text + "\n")
            print(f"Wrote {len(rows)} row(s) to {out}")
        else:
            print(text)
        return 0

    # delete
    key = argv[1].strip() if len(argv) > 1 else ""
    if not key:
        print("delete requires a <key>")
        return 2
    corpus.delete(key)
    print(f"Deleted {key}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
