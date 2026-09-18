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
    review_id_getter,
    stop_at_boundary: bool = True,
) -> list[dict]:
    """Select untracked reviews from a newest-first provider response.

    Only used on incremental (post-baseline) runs — the first run establishes a
    baseline and posts nothing (see ``collect_new_reviews``).

    ``stop_at_boundary`` must only be True when the provider sorts by an
    immutable key (Apple's createdDate). Google sorts by lastModified, which
    edits can bump above a genuinely-new review — breaking at the boundary
    there would shadow (and permanently drop) that review, so Google scans the
    whole fetched window and relies on posted_ids for dedup instead.
    """
    # posted_ids is the permanent dedup source (survives pruning); union it with
    # the active reviews map so nothing already posted is ever re-posted.
    known_ids = set(state.get("posted_ids", [])) | set(state.get("reviews", {}))
    last_review_id = state.get("last_review_id")
    if not last_review_id:
        # No boundary recorded (the app was baselined while it had zero reviews);
        # posted_ids is the only dedup source, so collect everything not seen.
        return [review for review in reviews if review_id_getter(review) not in known_ids]

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
    review_id_getter,
    normalizer,
    reply_sent_key: str,
    reply_sent_getter=None,
    stop_at_boundary: bool = True,
    suggestion_generator=None,
) -> list[dict]:
    """Select new reviews, record them in state, return dashboard entries.

    On the FIRST run for an app (``initial_sync``) nothing is posted: every
    review that exists at connection time is recorded as already-seen and the app
    is marked baselined, so only reviews that arrive AFTER connection are ever
    shown.

    On later runs, new reviews are recorded (posted_at + the reply flag +
    posted_ids), last_review_id advances, and ``normalizer(review,
    suggested_reply)`` dicts are returned for the app's pending-list data file.
    ``suggestion_generator`` is called ONCE with the list of new reviews and
    returns ``{review_id: reply}``; it must never raise (AI is optional).
    """
    if initial_sync:
        # Baseline only: mark every existing review as seen (posted_ids), record
        # the boundary, and flag the app baselined — but post nothing this run.
        for review in reviews:
            mark_posted(state, review_id_getter(review))
        if reviews:
            state["last_review_id"] = review_id_getter(reviews[0])
        state["baselined"] = True
        save_state(provider, state)
        LOG.info(
            "Baseline for %s: recorded %d existing review(s) as seen; posting none this run",
            provider,
            len(reviews),
        )
        return []

    new_reviews = select_new_reviews(reviews, state, review_id_getter, stop_at_boundary)
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
    return entries
