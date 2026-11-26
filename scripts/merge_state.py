"""Merge two provider state snapshots without losing either workflow's progress.

``merge_states`` is called by ``state_manager`` to reconcile a concurrent write
when an R2 conditional put is rejected (the sync and reply runs can both update
the same app's state object).
"""


def _timestamp(value: object) -> str:
    return value if isinstance(value, str) else ""


def merge_states(remote: dict, local: dict) -> dict:
    merged = dict(remote)
    merged.update({key: value for key, value in local.items() if key not in {"reviews", "last_review_id", "last_checked"}})

    remote_reviews = remote.get("reviews", {})
    local_reviews = local.get("reviews", {})
    reviews = {**remote_reviews, **local_reviews}
    for review_id in set(remote_reviews) & set(local_reviews):
        remote_entry = remote_reviews[review_id]
        local_entry = local_reviews[review_id]
        entry = {**remote_entry, **local_entry}
        # Preserve the newest reply (by replied_at) with its matching hash, so a
        # concurrent commit can't lose a reply that was just sent.
        replied_ats = [
            e for e in (remote_entry, local_entry) if isinstance(e.get("replied_at"), str)
        ]
        if replied_ats:
            newer = max(replied_ats, key=lambda candidate: _timestamp(candidate.get("replied_at")))
            entry["replied_at"] = newer["replied_at"]
            if "last_sent_reply_hash" in newer:
                entry["last_sent_reply_hash"] = newer["last_sent_reply_hash"]
            else:
                entry.pop("last_sent_reply_hash", None)
        # Any side's "replied sent" flag wins.
        for key in {k for k in set(remote_entry) | set(local_entry) if k.endswith("_reply_sent")}:
            entry[key] = bool(remote_entry.get(key) or local_entry.get(key))
        reviews[review_id] = entry
    merged["reviews"] = reviews

    # posted_ids is the permanent dedup set; never lose an id from either side.
    # Keep it newest-first without re-sorting: this run's local list carries the
    # freshly-posted ids on top, so take it first and append any remote id it is
    # missing, preserving each side's existing order.
    remote_posted = remote.get("posted_ids", []) or []
    local_posted = local.get("posted_ids", []) or []
    seen: set = set()
    merged_posted: list = []
    for review_id in list(local_posted) + list(remote_posted):
        if review_id not in seen:
            seen.add(review_id)
            merged_posted.append(review_id)
    merged["posted_ids"] = merged_posted

    if _timestamp(local.get("last_checked")) >= _timestamp(remote.get("last_checked")):
        merged["last_review_id"] = local.get("last_review_id", remote.get("last_review_id"))
        merged["last_checked"] = local.get("last_checked", remote.get("last_checked"))
    else:
        merged["last_review_id"] = remote.get("last_review_id", local.get("last_review_id"))
        merged["last_checked"] = remote.get("last_checked", local.get("last_checked"))
    return merged
