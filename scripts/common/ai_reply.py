"""Codex-backed suggested developer replies for store reviews.

Uses the org's Codex CLI (@openai/codex) authenticated by the shared ChatGPT
plan (CODEX_AUTH_JSON_BASE_64 restored to ~/.codex/auth.json by the workflow),
the same pattern as Lascade-Co/actions daily-catchup — so there is no per-token
OpenAI API key or billing.

Generation is an optional enhancement: every failure path returns an empty
result so the sync run itself never breaks because the AI is unavailable. All
of a run's new reviews are sent in ONE Codex invocation, which writes a JSON
object mapping each review id to its reply; the sync reads that file.
"""

import json
import logging
import os
import subprocess
import tempfile


LOG = logging.getLogger(__name__)

# Google Play rejects replies over ~350 chars; keep a margin and use the same
# budget for Apple so suggestions read consistently.
MAX_SUGGESTED_REPLY_LENGTH = 340
OUTPUT_FILENAME = "suggested_replies.json"
CODEX_TIMEOUT_SECONDS = 600

PROMPT_TEMPLATE = """You write the official public developer response to app store reviews.

For EVERY review in the JSON array at the end of this message, write a reply that:
- is written in the SAME language as the review's title and text — detect the
  language from the text itself, never from the reviewer's name or country; if a
  review is too short or ambiguous to tell, reply in English;
- is warm, professional, and concise; thanks the reviewer and addresses their
  specific points;
- never promises refunds, compensation, or delivery timelines;
- never requests or mentions personal data;
- never uses placeholders such as [NAME] or [APP];
- is at most {limit} characters.

Write ONLY a JSON object to a file named `{output}` in the current working
directory, mapping each review's "id" to its reply string. Do not print the
replies or any other commentary. Example: {{"abc123": "Thank you for ..."}}

Reviews:
{payload}
"""


def _codex_available() -> bool:
    # The workflow restores auth.json and installs the codex CLI; treat the
    # auth file's presence as the feature flag (matches the provider guards).
    return os.path.exists(os.path.expanduser("~/.codex/auth.json"))


def generate_suggested_replies(reviews: list[dict]) -> dict[str, str]:
    """Return {review_id: reply} for the given reviews (empty on any failure).

    ``reviews`` items are ``{"id", "platform", "rating", "title", "body"}``.
    """
    if not reviews:
        return {}
    if not _codex_available():
        LOG.info("Codex auth not present; skipping suggested replies")
        return {}

    payload = [
        {
            "id": str(review.get("id")),
            "platform": review.get("platform"),
            "rating": review.get("rating"),
            "title": review.get("title") or "",
            "body": review.get("body") or "",
        }
        for review in reviews
    ]
    prompt = PROMPT_TEMPLATE.format(
        limit=MAX_SUGGESTED_REPLY_LENGTH,
        output=OUTPUT_FILENAME,
        payload=json.dumps(payload, ensure_ascii=False, indent=2),
    )

    try:
        with tempfile.TemporaryDirectory() as scratch:
            subprocess.run(
                ["codex", "exec", "--sandbox", "workspace-write", "--skip-git-repo-check", "-"],
                input=prompt,
                cwd=scratch,
                check=True,
                capture_output=True,
                text=True,
                timeout=CODEX_TIMEOUT_SECONDS,
            )
            with open(os.path.join(scratch, OUTPUT_FILENAME), encoding="utf-8") as handle:
                data = json.load(handle)
    except Exception:
        LOG.warning("Codex suggested-reply generation failed; posting reviews without suggestions", exc_info=True)
        return {}

    if not isinstance(data, dict):
        LOG.warning("Codex suggested-reply output was not a JSON object; ignoring")
        return {}

    replies: dict[str, str] = {}
    for review_id, reply in data.items():
        if isinstance(reply, str) and reply.strip():
            text = reply.strip()
            replies[str(review_id)] = text[:MAX_SUGGESTED_REPLY_LENGTH].rstrip()
    LOG.info("Codex generated %d suggested repl(ies)", len(replies))
    return replies
