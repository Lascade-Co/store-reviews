"""Provider-neutral review selection, posting, and Slack reply polling helpers."""

import hashlib
import logging
import re

from common.slack_client import (
    SlackApiError,
    SlackClient,
    SlackPermissionError,
    SlackThreadNotFoundError,
)
from common.state_manager import mark_posted, now_iso, save_state, upsert_review


LOG = logging.getLogger(__name__)


def reply_hash(text: str) -> str:
    """Return a stable hash for the normalized response text."""
    return hashlib.sha256(text.strip().encode("utf-8")).hexdigest()


# The Slack review message is the ONLY store of the suggested reply (state is
# in a public repo, so unapproved AI text is deliberately not persisted there).
# Extraction below parses the text between these two markers, so formatter and
# extractor must always use the same constants.
_SUGGESTION_PREFIX = "💡 *Suggested Reply:* "
_SUGGESTION_HINT = (
    "_React to this message (any emoji) to send the suggested reply, "
    "or type your own reply in this thread._"
)


def format_suggestion_section(suggested_reply: str | None, escape) -> str:
    """Return the 💡 block appended to a review message, or an empty string."""
    if not suggested_reply:
        return ""
    return f"\n{_SUGGESTION_PREFIX}{escape(suggested_reply)}\n\n{_SUGGESTION_HINT}\n"


def extract_suggested_reply(parent_message: object) -> str | None:
    """Recover the suggested reply from the posted Slack review message.

    Slack returns the message with our entity escaping intact and bare URLs
    auto-wrapped in angle brackets; decode_slack_text reverses both.
    """
    if not isinstance(parent_message, dict):
        return None
    text = parent_message.get("text")
    if not isinstance(text, str) or _SUGGESTION_PREFIX not in text:
        return None
    suggestion = text.split(_SUGGESTION_PREFIX, 1)[1]
    suggestion = suggestion.split(_SUGGESTION_HINT, 1)[0]
    # Drop the dashed footer if the hint line was somehow absent.
    suggestion = suggestion.split("\n-----------", 1)[0]
    suggestion = decode_slack_text(suggestion).strip()
    return suggestion or None


def has_human_reaction(parent_message: object, bot_user_id: str | None) -> bool:
    """Return whether any non-bot user reacted (any emoji) to the message."""
    if not isinstance(parent_message, dict):
        return False
    reactions = parent_message.get("reactions")
    if not isinstance(reactions, list):
        return False
    for reaction in reactions:
        if not isinstance(reaction, dict):
            continue
        users = [user for user in (reaction.get("users") or []) if isinstance(user, str)]
        if any(user != bot_user_id for user in users):
            return True
        # Slack may truncate the users list for popular reactions while count
        # stays accurate; the bot never reacts, so uncounted reactors are human.
        count = reaction.get("count")
        if isinstance(count, int) and count > len(users):
            return True
    return False


_SLACK_BRACKET_RE = re.compile(r"<([^<>]*)>")


def _unwrap_slack_bracket(match: re.Match) -> str:
    inner = match.group(1)
    if "|" in inner:
        # <https://x|label>, <@U1|alice>, <#C1|general> → keep the label.
        return inner.split("|", 1)[1]
    # <@U123>, <#C123>, <!here> → drop the sigil; <https://x> → keep the URL.
    return inner.lstrip("@#!")


def decode_slack_text(text: str) -> str:
    """Convert Slack message markup into plain text for the store response.

    Slack's `text` HTML-escapes & < > and wraps links, mentions, and commands
    in angle brackets. Sent verbatim, a developer reply would show literal
    `&amp;` or `<https://x|x>` publicly, so unwrap brackets first, then
    unescape entities (&amp; last so it cannot re-introduce markup).
    """
    text = _SLACK_BRACKET_RE.sub(_unwrap_slack_bracket, text or "")
    return text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


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


def post_new_reviews(
    provider: str,
    reviews: list[dict],
    state: dict,
    slack: SlackClient,
    initial_sync: bool,
    initial_count: int,
    review_id_getter,
    formatter,
    reply_sent_key: str,
    reply_sent_getter=None,
    stop_at_boundary: bool = True,
    baseline_all_fetched: bool = False,
    suggestion_generator=None,
) -> None:
    """Post selected reviews oldest-first and persist each Slack mapping.

    ``baseline_all_fetched`` (used by Google, whose selection has no boundary
    stop): on the initial sync, record EVERY fetched review id in posted_ids —
    not just the few that were posted — so the next run treats the rest of the
    window as already seen and posts only reviews that appear afterwards.

    ``suggestion_generator(review)`` may return an AI-suggested reply; it is
    shown in the Slack message (and later recovered from it when a reaction
    approves sending it to the store). It must never raise.
    """
    new_reviews = select_new_reviews(
        reviews, state, initial_sync, initial_count, review_id_getter, stop_at_boundary
    )
    if new_reviews:
        LOG.info("Sending %d new %s review(s) to Slack", len(new_reviews), provider)
        for review in reversed(new_reviews):
            review_id = review_id_getter(review)
            suggested_reply = suggestion_generator(review) if suggestion_generator else None
            # The suggestion lives ONLY in the Slack message (recovered from it
            # at approval time); it is never persisted in this public repo.
            slack_ts = slack.post_review(formatter(review, suggested_reply))
            upsert_review(
                state,
                review_id,
                slack_ts=slack_ts,
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
            LOG.info("Posted %s review %s to Slack thread %s", provider, review_id, slack_ts)
        state["last_review_id"] = review_id_getter(reviews[0])
        save_state(provider, state)
    else:
        LOG.info("No new %s reviews to send", provider)
        # Escape a stuck initial sync: if every fetched review is already known
        # but the boundary was never recorded, set it so the next run goes
        # incremental (and polls replies).
        if reviews and not state.get("last_review_id"):
            state["last_review_id"] = review_id_getter(reviews[0])
            save_state(provider, state)

    if initial_sync and baseline_all_fetched and reviews:
        skipped = 0
        posted = set(state.get("posted_ids", []))
        for review in reviews:
            review_id = review_id_getter(review)
            if review_id not in posted:
                mark_posted(state, review_id)
                posted.add(review_id)
                skipped += 1
        if skipped:
            save_state(provider, state)
            LOG.info(
                "Baselined %d pre-existing %s review(s); they are marked as seen and will never be posted",
                skipped,
                provider,
            )


def reply_candidates(messages: list[dict], state_entry: dict, slack: SlackClient) -> list[dict]:
    """Return unique, newer, non-bot, non-system, non-empty human replies."""
    last_reply_ts = state_entry.get("last_reply_ts")
    candidates = []
    seen_timestamps = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        ts = message.get("ts")
        if not isinstance(ts, str) or ts in seen_timestamps or (last_reply_ts and ts <= last_reply_ts):
            continue
        seen_timestamps.add(ts)
        if not slack.is_human_message(message):
            continue
        text = (message.get("text") or "").strip()
        if text:
            candidates.append(message)
    return sorted(candidates, key=lambda message: message["ts"])


def sync_slack_replies(
    provider: str,
    state: dict,
    slack: SlackClient,
    reply_sent_key: str,
    send_reply,
    display_name: str | None = None,
) -> None:
    """Apply the newest human Slack reply as the provider's single response."""
    display_name = display_name or provider
    reviews = state.get("reviews", {})
    if not reviews:
        LOG.info("No %s review threads to poll", display_name)
        return

    slack.identify_bot()
    failures = []
    for review_id, entry in reviews.items():
        if entry.get("slack_thread_disabled"):
            LOG.info("Skipping %s review %s: Slack thread disabled", display_name, review_id)
            continue
        if not entry.get("slack_ts"):
            LOG.warning("%s review %s has no Slack timestamp; skipping", display_name, review_id)
            continue

        try:
            LOG.info("Polling Slack thread %s for %s review %s", entry["slack_ts"], display_name, review_id)
            messages = slack.replies(entry["slack_ts"])
            if len(messages) > 1:
                LOG.info(
                    "Slack thread for %s review %s contains %d message(s) including the parent",
                    display_name,
                    review_id,
                    len(messages),
                )
            candidates = reply_candidates(messages, entry, slack)
            if not candidates:
                # No typed reply — check for reaction approval of the AI
                # suggestion. Reactions carry no timestamp, so they only count
                # while NO response was ever sent for this review; afterwards
                # the thread's typed replies own all updates (a developer can
                # change the sent suggestion within the edit window just by
                # typing in the thread, without removing the reaction).
                parent_message = next(
                    (
                        message
                        for message in messages
                        if isinstance(message, dict) and message.get("ts") == entry["slack_ts"]
                    ),
                    None,
                )
                if not entry.get("last_sent_reply_hash") and has_human_reaction(
                    parent_message, slack.bot_user_id
                ):
                    # The posted Slack message is the source of truth for the
                    # suggestion (state lives in a public repo and must not
                    # carry unapproved AI text).
                    suggested_reply = extract_suggested_reply(parent_message)
                    if not suggested_reply:
                        LOG.info(
                            "Reaction found for %s review %s but its message has no suggested reply; skipping",
                            display_name,
                            review_id,
                        )
                        continue
                    LOG.info(
                        "Reaction approval: sending suggested reply to %s review %s",
                        display_name,
                        review_id,
                    )
                    send_reply(review_id, suggested_reply)
                    entry["last_sent_reply_hash"] = reply_hash(suggested_reply)
                    entry["replied_at"] = now_iso()
                    entry[reply_sent_key] = True
                    save_state(provider, state)
                    LOG.info(
                        "Posted suggested reply to %s review %s via reaction approval",
                        display_name,
                        review_id,
                    )
                    continue
                LOG.debug("No new human Slack reply found for %s review %s", display_name, review_id)
                continue

            message = candidates[-1]
            LOG.info("Found new human Slack reply %s for %s review %s", message["ts"], display_name, review_id)
            normalized_text = decode_slack_text(message["text"]).strip()
            if not normalized_text:
                LOG.debug("Newest Slack reply for %s review %s is empty after decoding; skipping", display_name, review_id)
                continue
            message_hash = reply_hash(normalized_text)
            if entry.get("last_sent_reply_hash") == message_hash:
                # Advance the timestamp so this already-applied reply is not
                # reconsidered, without making another provider API call.
                entry["last_reply_ts"] = message["ts"]
                entry[reply_sent_key] = True
                save_state(provider, state)
                LOG.info(
                    "Skipping %s review %s: newest Slack reply matches the response already sent",
                    display_name,
                    review_id,
                )
                continue
            LOG.info("Sending Slack reply %s to %s review %s", message["ts"], display_name, review_id)
            send_reply(review_id, normalized_text)
            entry["last_reply_ts"] = message["ts"]
            entry["last_sent_reply_hash"] = message_hash
            entry["replied_at"] = now_iso()
            entry[reply_sent_key] = True
            save_state(provider, state)
            LOG.info("Posted Slack reply %s to %s review %s", message["ts"], display_name, review_id)
        except SlackThreadNotFoundError as exc:
            entry["slack_thread_disabled"] = True
            save_state(provider, state)
            LOG.error("Slack thread for %s review %s was deleted or unavailable: %s", display_name, review_id, exc)
            failures.append(review_id)
        except SlackPermissionError:
            LOG.exception("Slack permission failure for %s review %s", display_name, review_id)
            raise
        except SlackApiError as exc:
            LOG.error("Slack API failure for %s review %s: %s", display_name, review_id, exc)
            if exc.error == "rate_limited":
                LOG.error("Slack rate limit was exhausted after Retry-After retries")
                raise
            failures.append(review_id)
        except Exception as exc:
            LOG.exception("Failed processing %s reply for review %s: %s", display_name, review_id, exc)
            failures.append(review_id)

    if failures:
        raise RuntimeError(
            f"{provider.title()} review synchronization failed for {len(failures)} review(s): "
            f"{', '.join(failures)}"
        )
