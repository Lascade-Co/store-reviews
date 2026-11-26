"""Prune inactive reviews from state to keep the active set bounded.

The `reviews` map holds only reviews still worth showing on the dashboard. This
drops the rest while keeping their ids in `posted_ids` (the permanent dedup
set), so a pruned review is never re-published.

Rules (a review is kept only if it is still "active"):
  - replied:   keep for REPLY_EDIT_WINDOW_DAYS after the reply, then drop
  - un-replied: keep for OPEN_POLL_WINDOW_DAYS after it was posted, then drop

``prune_inactive`` is called by ``state_manager.prune_and_save`` just before the
R2 write — AFTER any merge, so the union can't resurrect a pruned entry.
"""

from datetime import datetime, timezone


OPEN_POLL_WINDOW_DAYS = 7
REPLY_EDIT_WINDOW_DAYS = 2


def _age_days(timestamp: object, now: datetime) -> float:
    """Age in days of an ISO-8601 timestamp. Unparseable/missing → 0 (treat as now → keep)."""
    if not isinstance(timestamp, str) or not timestamp.strip():
        return 0.0
    try:
        value = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return (now - value).total_seconds() / 86400.0


def prune_inactive(state: dict, now: datetime) -> dict:
    """Return state with inactive reviews removed from `reviews` (ids kept in `posted_ids`)."""
    all_review_ids = list((state.get("reviews", {}) or {}).keys())
    kept = {}
    for review_id, entry in (state.get("reviews", {}) or {}).items():
        if not isinstance(entry, dict):
            continue
        if entry.get("replied_at"):
            if _age_days(entry.get("replied_at"), now) <= REPLY_EDIT_WINDOW_DAYS:
                kept[review_id] = entry  # replied recently → keep briefly
        else:
            if _age_days(entry.get("posted_at"), now) <= OPEN_POLL_WINDOW_DAYS:
                kept[review_id] = entry  # open & recent → keep on the dashboard
    state["reviews"] = kept

    # posted_ids is the permanent dedup set, kept newest-first so the oldest ids
    # sit at the bottom and can be trimmed by hand later. Preserve the existing
    # order untouched (do NOT re-sort); never lose a review's id — any active id
    # not already recorded is a recent review, so it goes on top.
    existing = state.get("posted_ids", []) or []
    seen = set(existing)
    missing = [rid for rid in all_review_ids if rid not in seen]
    state["posted_ids"] = missing + list(existing)
    return state
