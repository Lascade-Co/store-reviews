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
import re
import subprocess
import tempfile


LOG = logging.getLogger(__name__)

# Leading ISO-8601 timestamp Codex prefixes to each stderr line. Stripped before
# deduping so the same message logged 300× (once per retry) collapses to one line.
_TIMESTAMP_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T[\d:.]+Z\s+")


def _summarize_stream(text: object, max_lines: int = 25, max_chars: int = 4000) -> str:
    """Collapse a noisy Codex stream into unique messages with repeat counts.

    On an auth/transport failure Codex retries internally and prints the same
    error hundreds of times, each with a fresh timestamp. Strip the timestamp,
    keep each distinct message once in first-seen order with an ``(xN)`` count,
    and cap the size so a retry storm becomes a few readable lines instead of a
    thousand-line dump.
    """
    if isinstance(text, bytes):
        text = text.decode("utf-8", "replace")
    if not isinstance(text, str) or not text.strip():
        return "(empty)"

    counts: dict[str, int] = {}
    order: list[str] = []
    for raw in text.splitlines():
        line = _TIMESTAMP_RE.sub("", raw.strip()).strip()
        if not line:
            continue
        if line not in counts:
            order.append(line)
        counts[line] = counts.get(line, 0) + 1

    rendered = [f"(x{counts[line]}) {line}" if counts[line] > 1 else line for line in order[:max_lines]]
    if len(order) > max_lines:
        rendered.append(f"... (+{len(order) - max_lines} more distinct line(s))")
    summary = "\n".join(rendered)
    if len(summary) > max_chars:
        summary = summary[:max_chars] + "\n... (truncated)"
    return summary

# Google Play rejects replies over ~350 chars; keep a margin and use the same
# budget for Apple so suggestions read consistently.
MAX_SUGGESTED_REPLY_LENGTH = 340
OUTPUT_FILENAME = "suggested_replies.json"
CODEX_TIMEOUT_SECONDS = 600

PROMPT_TEMPLATE = """You write official public developer replies to app store reviews.

For EVERY review:
- Reply in the SAME language as the review text; if unclear, use English.
- Be warm, natural, professional, and concise.
- Address the review's main point without simply repeating or summarizing it.
- For detailed reviews, mention only 1–2 relevant points naturally; do not list every feature/detail.
- For rating-only reviews, give a short thank-you and do not invent reasons for the rating.
- Avoid AI-sounding, repetitive, overly enthusiastic, or promotional language.
- Never invent facts, features, fixes, refunds, compensation, or delivery timelines.
- Never request or mention personal data.
- Never use placeholders such as [NAME] or [APP].
- Keep the reply at most {limit} characters.

Write ONLY a JSON object to a file named `{output}` mapping each review's "id" to its reply string. Do not print replies or any other commentary.

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
    except subprocess.CalledProcessError as exc:
        # capture_output stores Codex's own output on the exception but never
        # prints it. Surface the real cause (auth, sandbox, usage limit) from
        # stderr, deduplicated — Codex echoes the whole prompt to stdout and
        # repeats each error per retry, so we log neither raw.
        detail = exc.stderr if (exc.stderr and str(exc.stderr).strip()) else exc.stdout
        LOG.warning(
            "Codex failed (exit %s); posting reviews without suggestions.\n--- codex error ---\n%s",
            exc.returncode,
            _summarize_stream(detail),
        )
        return {}
    except subprocess.TimeoutExpired as exc:
        LOG.warning(
            "Codex timed out after %ss; posting reviews without suggestions.\n--- codex error ---\n%s",
            CODEX_TIMEOUT_SECONDS,
            _summarize_stream(exc.stderr),
        )
        return {}
    except FileNotFoundError:
        LOG.warning(
            "Codex ran but wrote no %s; posting reviews without suggestions", OUTPUT_FILENAME, exc_info=True
        )
        return {}
    except json.JSONDecodeError as exc:
        LOG.warning(
            "Codex output was not valid JSON (%s); posting reviews without suggestions", exc, exc_info=True
        )
        return {}
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
