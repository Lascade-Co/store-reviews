"""Provider-neutral review selection and collection for the web dashboard."""

import hashlib
import logging

from common.state_manager import mark_posted, now_iso, save_state, upsert_review


LOG = logging.getLogger(__name__)


def reply_hash(text: str) -> str:
    """Return a stable hash for the normalized response text."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


def select_new_reviews(
    reviews: list[dict],
    state: dict,
    initial_sync: bool,
    initial_count: int,
    review_id_getter,
    stop_at_boundary: bool = True,
) -> list[dict]:
    """Select untracked reviews from a newest-first provider response.

    ``stop_at_boundary`` must only be True when the provider sorts by an
    immutable key (Apple's createdDate). Google sorts by lastModified, which
    edits can bump above a genuinely-new review — breaking at the boundary
    there would shadow (and permanently drop) that review, so Google scans the
    whole fetched window and relies on posted_ids for dedup instead.
    """
    # posted_ids is the permanent dedup source (survives pruning); union it with
    # the active reviews map so nothing already posted is ever re-posted.
    known_ids = set(state.get("posted_ids", [])) | set(state.get("reviews", {}))
    if initial_sync:
        return [
            review
            for review in reviews[:initial_count]
            if review_id_getter(review) not in known_ids
        ]

    last_review_id = state.get("last_review_id")
    if not last_review_id:
        raise RuntimeError("Incremental sync requires last_review_id")

    new_reviews = []
    boundary_found = False
    for review in reviews:
        review_id = review_id_getter(review)
        if review_id == last_review_id:
            boundary_found = True
            if stop_at_boundary:
                break
            continue
        if review_id not in known_ids:
            new_reviews.append(review)
    if stop_at_boundary and not boundary_found:
        LOG.warning("last_review_id %s was not present in the fetched review set", last_review_id)
    return new_reviews


def collect_new_reviews(
    provider: str,
    reviews: list[dict],
    state: dict,
    initial_sync: bool,
    initial_count: int,
    review_id_getter,
    normalizer,
    reply_sent_key: str,
    reply_sent_getter=None,
    stop_at_boundary: bool = True,
    baseline_all_fetched: bool = False,
    suggestion_generator=None,
) -> list[dict]:
    """Select new reviews, record them in state, return dashboard entries.

    Records posted_at + the reply flag + posted_ids and advances
    last_review_id, then returns ``normalizer(review, suggested_reply)`` dicts
    for the app's pending-list data file. ``suggestion_generator`` is called
    ONCE with the list of new reviews and returns ``{review_id: reply}``; it
    must never raise (AI is an optional enhancement).

    ``baseline_all_fetched`` (used by Google, whose selection has no boundary
    stop): on the initial sync, record EVERY fetched review id in posted_ids —
    not just the few that were posted — so the next run treats the rest of the
    window as already seen.
    """
    new_reviews = select_new_reviews(
        reviews, state, initial_sync, initial_count, review_id_getter, stop_at_boundary
    )
    suggestions = suggestion_generator(new_reviews) if (new_reviews and suggestion_generator) else {}
    entries: list[dict] = []
    if new_reviews:
        LOG.info("Collected %d new %s review(s) for the dashboard", len(new_reviews), provider)
        for review in reversed(new_reviews):
            review_id = review_id_getter(review)
            suggested_reply = suggestions.get(review_id)
            upsert_review(
                state,
                review_id,
                last_reply_ts=None,
                posted_at=now_iso(),
                **{
                    reply_sent_key: (
                        bool(reply_sent_getter(review)) if reply_sent_getter else False
                    )
                },
            )
            mark_posted(state, review_id)
            save_state(provider, state)
            entries.append(normalizer(review, suggested_reply))
        state["last_review_id"] = review_id_getter(reviews[0])
        save_state(provider, state)
    else:
        LOG.info("No new %s reviews to collect", provider)
        # Escape a stuck initial sync: if every fetched review is already known
        # but the boundary was never recorded, set it so the next run goes
        # incremental.
        if reviews and not state.get("last_review_id"):
            state["last_review_id"] = review_id_getter(reviews[0])
            save_state(provider, state)

    if initial_sync and baseline_all_fetched and reviews:
        posted = set(state.get("posted_ids", []))
        changed = False
        for review in reviews:
            review_id = review_id_getter(review)
            if review_id not in posted:
                mark_posted(state, review_id)
                posted.add(review_id)
                changed = True
        if changed:
            save_state(provider, state)
    return entries
