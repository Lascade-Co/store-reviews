"""OpenAI-backed suggested developer replies for store reviews.

Generation is an optional enhancement: every failure path returns None so the
sync run itself can never break because the AI is unavailable. The suggestion
is generated once when a review is posted to Slack and stored in state; a
Slack reaction on the review message later approves sending it to the store.
"""

import json
import logging
import os

from common.utils import request_with_retries


LOG = logging.getLogger(__name__)

OPENAI_API_URL = "https://api.openai.com/v1/chat/completions"
# gpt-5.6-luna is the current-generation efficient model: best small-tier
# quality (multilingual included) at $0.20/$1.20 per 1M tokens.
DEFAULT_OPENAI_MODEL = "gpt-5.6-luna"
# Google Play rejects replies over ~350 characters; keep a safety margin and
# use the same budget for Apple so suggestions read consistently.
MAX_SUGGESTED_REPLY_LENGTH = 340

SYSTEM_PROMPT = (
    "You write the official public developer response to an app store review. "
    "First, identify the language the review title and text are actually written in; "
    "write the entire reply in exactly that language. Never guess the language from "
    "the reviewer's name or country. If the review is too short or ambiguous to "
    "determine a language, reply in English. "
    "Reply warmly, professionally, and concisely. "
    "Thank the reviewer and address their specific points. "
    "Never promise refunds, compensation, or delivery timelines. "
    "Never request or mention personal data. "
    "Never use placeholders such as [NAME] or [APP]. "
    f"The reply must be at most {MAX_SUGGESTED_REPLY_LENGTH} characters."
)

_RESPONSE_FORMAT = {
    "type": "json_schema",
    "json_schema": {
        "name": "suggested_reply",
        "strict": True,
        "schema": {
            "type": "object",
            "properties": {"reply": {"type": "string"}},
            "required": ["reply"],
            "additionalProperties": False,
        },
    },
}


def _extract_reply(data: object) -> str | None:
    """Pull the reply text out of a chat-completions response, or None."""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return None
    if not isinstance(content, str) or not content.strip():
        return None
    try:
        parsed = json.loads(content)
        reply = parsed.get("reply") if isinstance(parsed, dict) else None
    except ValueError:
        # Defensive fallback: treat non-JSON content as the reply itself.
        reply = content
    if not isinstance(reply, str):
        return None
    reply = reply.strip().strip('"').strip()
    return reply or None


def generate_suggested_reply(platform: str, rating: object, title: str, body: str) -> str | None:
    """Return a suggested developer reply for a review, or None when unavailable."""
    api_key = os.environ.get("OPENAI_API_KEY", "").strip()
    if not api_key:
        LOG.debug("OPENAI_API_KEY not set; skipping suggested reply generation")
        return None

    review_text = (
        f"Platform: {platform}\n"
        f"Rating: {rating}/5\n"
        f"Title: {title}\n"
        f"Review: {body}"
    )
    payload = {
        "model": os.environ.get("OPENAI_MODEL", "").strip() or DEFAULT_OPENAI_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": review_text},
        ],
        # gpt-5.6-luna is a REASONING model that defaults to medium effort:
        # hidden reasoning tokens count against max_completion_tokens (and are
        # billed as output). Keep reasoning low and leave generous headroom so
        # the JSON answer is never truncated away.
        "reasoning_effort": "low",
        "max_completion_tokens": 1536,
        "response_format": _RESPONSE_FORMAT,
    }

    try:
        # Generation is idempotent, so the shared retry policy is safe here:
        # network errors and 5xx retry with backoff, and 429 rate limits sleep
        # exactly the Retry-After header before retrying (up to 3 retries).
        # A rejected 429 request is not billed. On final failure the review is
        # simply posted without a suggestion.
        response = request_with_retries(
            "POST",
            OPENAI_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=60,
            operation="OpenAI suggested reply",
        )
        if not response.ok:
            LOG.warning(
                "OpenAI suggested reply failed: http_status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            return None
        reply = _extract_reply(response.json())
        if not reply:
            LOG.warning("OpenAI suggested reply response contained no usable reply text")
            return None
        if len(reply) > MAX_SUGGESTED_REPLY_LENGTH:
            reply = reply[:MAX_SUGGESTED_REPLY_LENGTH].rstrip()
        return reply
    except Exception:
        LOG.warning("OpenAI suggested reply generation failed; posting review without a suggestion", exc_info=True)
        return None
