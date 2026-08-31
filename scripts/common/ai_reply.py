"""Groq-backed suggested developer replies for store reviews.

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

GROQ_API_URL = "https://api.groq.com/openai/v1/chat/completions"
# gpt-oss models are Groq's current production text models with strict
# structured-output support (the llama-3.x models are deprecated on Groq).
DEFAULT_GROQ_MODEL = "openai/gpt-oss-20b"
# Google Play rejects replies over ~350 characters; keep a safety margin and
# use the same budget for Apple so suggestions read consistently.
MAX_SUGGESTED_REPLY_LENGTH = 340

SYSTEM_PROMPT = (
    "You write the official public developer response to an app store review. "
    "Reply warmly, professionally, and concisely, in the same language as the review. "
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
        # Defensive fallback: a model without strict schema support may return
        # the reply as plain text.
        reply = content
    if not isinstance(reply, str):
        return None
    reply = reply.strip().strip('"').strip()
    return reply or None


def generate_suggested_reply(platform: str, rating: object, title: str, body: str) -> str | None:
    """Return a suggested developer reply for a review, or None when unavailable."""
    api_key = os.environ.get("GROQ_API_KEY", "").strip()
    if not api_key:
        LOG.debug("GROQ_API_KEY not set; skipping suggested reply generation")
        return None

    review_text = (
        f"Platform: {platform}\n"
        f"Rating: {rating}/5\n"
        f"Title: {title}\n"
        f"Review: {body}"
    )
    payload = {
        "model": os.environ.get("GROQ_MODEL", "").strip() or DEFAULT_GROQ_MODEL,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": review_text},
        ],
        "temperature": 0.4,
        "max_completion_tokens": 400,
        "response_format": _RESPONSE_FORMAT,
    }

    try:
        # Generation is idempotent, so the shared retry policy (network, 429
        # with retry-after, 5xx) is safe to apply.
        response = request_with_retries(
            "POST",
            GROQ_API_URL,
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            json=payload,
            timeout=45,
            operation="Groq suggested reply",
        )
        if not response.ok:
            LOG.warning(
                "Groq suggested reply failed: http_status=%s body=%s",
                response.status_code,
                response.text[:300],
            )
            return None
        reply = _extract_reply(response.json())
        if not reply:
            LOG.warning("Groq suggested reply response contained no usable reply text")
            return None
        if len(reply) > MAX_SUGGESTED_REPLY_LENGTH:
            reply = reply[:MAX_SUGGESTED_REPLY_LENGTH].rstrip()
        return reply
    except Exception:
        LOG.warning("Groq suggested reply generation failed; posting review without a suggestion", exc_info=True)
        return None
