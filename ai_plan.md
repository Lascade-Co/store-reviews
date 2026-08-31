# AI Suggested Replies — Plan (branch `ai_integrated`)

## 1. Feature summary

After fetching reviews, the provider job asks an LLM (Groq, free tier) for a short suggested
developer response. The Slack review message gains a **💡 Suggested Reply** section. A developer
approves it by adding **any emoji reaction on the review message** (zero extra API calls — the
decision, accepted with the known tradeoff that casual reactions like 😂/👀 also count as
approval). Typing a normal reply in the thread still works exactly as today and always wins over
the suggestion.

```
🍎 New Appstore Review
Rating: 2/5 ... (existing fields)

💡 Suggested Reply: We're sorry about the pricing surprise ...

React to this message (any emoji) to send the suggested reply,
or type your own reply in the thread.
-----------
```

## 2. Verified facts the design relies on

| Fact | Status |
|---|---|
| `conversations.replies` parent message includes `reactions: [{name, count, users[]}]` | VERIFIED — no extra call, no new scope (history scopes suffice; `reactions:read` NOT needed) |
| Reaction names arrive as base names + optional `::skin-tone-N` | VERIFIED — irrelevant under any-emoji rule (we only check "at least one non-bot reactor") |
| `users[]` in reactions lets us ignore the bot's own reactions | VERIFIED (compare to `auth.test` user id) |
| Groq endpoint: `POST https://api.groq.com/openai/v1/chat/completions`, `Authorization: Bearer` | VERIFIED (OpenAI-compatible; use `max_completion_tokens`) |
| Model: `openai/gpt-oss-20b` (fallback `openai/gpt-oss-120b`) — llama-3.x models are DEPRECATED (shutdown 08/16/26) | VERIFIED on deprecations page |
| Free tier: ~30 req/min, 1K req/day, 8K tokens/min; 429 + `retry-after` header; no credit card | VERIFIED |
| Structured output `json_schema` + `strict:true` supported on gpt-oss models | VERIFIED |

## 3. Design

### 3.1 New module — `scripts/common/ai_reply.py`
- `generate_suggested_reply(review: NormalizedReview, platform: str) -> str | None`
- Groq call via existing `request_with_retries` (429 honors `retry-after`; generation is
  idempotent so retries are safe). Timeout ~30s.
- Model from `GROQ_MODEL` env (default `openai/gpt-oss-20b`); key from `GROQ_API_KEY`.
- Prompt (system): support-agent persona; warm, professional, concise; **reply in the review's
  language**; no promises/compensation offers; no personal data; no placeholders like [NAME];
  ≤ 340 characters (fits Google's 350 limit; used for Apple too, for consistency).
- Use `response_format: json_schema, strict: true` → `{"reply": "..."}` (guaranteed shape on
  gpt-oss), fall back to plain text parse if the API rejects it.
- **Failure-safe:** any error (no key, 429 exhausted, timeout, bad response) → `LOG.warning`,
  return `None`. The review is then posted WITHOUT a suggestion — AI must never fail the sync.
- Skip generation entirely when `GROQ_API_KEY` is absent (feature flag by omission, same
  pattern as the provider guards).

### 3.2 Posting flow (`post_new_reviews` in `review_sync.py`)
- Before `slack.post_review`: `suggestion = generate_suggested_reply(...)`.
- Formatter gains an optional `suggested_reply` argument; when present, appends the 💡 section +
  approval hint (suggestion text Slack-escaped like all user content).
- State entry gains `"suggested_reply": <plain text>` (needed on later runs to know what to send
  when approval arrives). Stored decoded/plain — it is already store-ready.
- Volume check: worst case = initial sync 5 reviews × 2 platforms = 10 Groq calls per app run,
  far under 30/min & 1K/day. Multi-app runs are already staggered.

### 3.3 Approval detection (`sync_slack_replies` in `review_sync.py`)
Per active thread, decide the outbound response with this priority:

1. **Typed human reply (newest)** → send it. Existing behavior, always wins.
2. **Any reaction on the parent message from any non-bot user** (any emoji; the parent is
   `conversations.replies`' first message, its `reactions` array is checked for at least one
   entry whose `users` include a non-bot user): only considered when **no response was ever
   sent** for this review (`last_sent_reply_hash` is empty). Reactions have no timestamp, so
   gating on "never replied" keeps them from fighting the newest-typed-reply-wins rule — once
   any reply (typed or suggested) has been sent, reactions are ignored for that review forever,
   which also makes "change the reply within 2 days by typing in the thread" work without
   removing the reaction.

For rule 2: if `suggested_reply` is missing (AI was down at post time) → log
"reaction found but no stored suggestion" and skip. Send path, hashing (`last_sent_reply_hash` of
the suggestion), `replied_at`, edit-window behavior, and failure handling are all the existing
machinery — the suggestion is just another reply text.

### 3.4 Reaction helper
- `has_human_reaction(parent_message, bot_user_id)` → True when any `reactions[]` entry has a
  user other than the bot. No emoji-name filtering (any emoji approves, by decision). Skin-tone
  variants are irrelevant under this rule.

### 3.5 Secrets & workflow
- `GROQ_API_KEY`: **central repo GitHub secret** (shared service, same pattern as
  `SLACK_BOT_TOKEN`); added to the sync step env. `GROQ_MODEL` optional plain env in the workflow.
- No Infisical change, no new Slack scope, no trigger change.

### 3.6 State
- Additive only (`suggested_reply` on entries); no version bump, no migration (missing key =
  no suggestion). Pruning/merge untouched — `merge_state.py` unions entries and the field rides
  along (union keeps it since local entry wins on conflict).

## 4. Edge cases

| Case | Behavior |
|---|---|
| Groq down / key missing | Review posted without 💡 section; everything else normal |
| Approval but no stored suggestion | Logged, skipped — nothing sent |
| Reaction added after any reply was sent | Ignored (rule 2 requires never-replied) — so "react, then change your mind and type in the thread within 2 days" replaces the suggestion without touching the reaction |
| Typed reply after suggestion was sent via approval | Newest-wins replaces it (within 2-day window) |
| Both a reaction and a typed reply on first poll | Typed reply wins (priority 1) |
| Bot reacts to its own message | Ignored via `users[]` vs `auth.test` id |
| Suggestion > 350 chars (model ignored limit) | Existing `_prepare_reply` truncation backstop |
| Reaction `users` list truncated (huge reaction counts) | Irrelevant — any non-bot reactor approves; count≥1 with non-bot user suffices |
| Non-English review | Prompt requires reply in the review's language |

## 5. Files to change

| File | Change |
|---|---|
| `scripts/common/ai_reply.py` | **new** — Groq client + prompt + failure-safe wrapper |
| `scripts/common/review_sync.py` | generate on post; store `suggested_reply`; approval detection (priority 1/2/3); emoji helpers |
| `scripts/providers/appstore.py` | formatter: optional 💡 section; pass suggestion through |
| `scripts/providers/playstore.py` | same |
| `.github/workflows/review-sync.yml` | `GROQ_API_KEY` env (+ optional `GROQ_MODEL`) on sync step |
| `tests/test_ai_reply.py` | **new** — mocked Groq: success, failure→None, length clamp, no-key skip |
| `tests/test_review_sync.py` | approval matrix: reaction sends suggestion once; typed reply beats reaction; reaction ignored after any reply was sent (change-within-2-days flow); bot-only reaction ignored; reaction w/o stored suggestion skips |
| `review.md`, `state/README.md` | usage + field docs (after implementation) |

## 6. Rollout

1. Implement + tests on `ai_integrated`.
2. Add `GROQ_API_KEY` repo secret (console.groq.com → API Keys; free, no card).
3. Manual `workflow_dispatch` from the branch?  — central workflow runs from `main` on dispatch,
   so test by temporarily pointing a manual run at the branch (Actions → run workflow → select
   branch `ai_integrated`) — `workflow_dispatch` supports choosing the branch, and state commits
   go to that branch's `state/` (verify then discard or merge).
4. Verify in Slack: 💡 section renders; a reaction sends the suggestion to the store; typing in
   the thread afterwards (within 2 days) replaces it; typed-only flow still works.
5. Merge `ai_integrated` → `main`.

## 7. Out of scope (later ideas)

- Per-app prompt context (app name/tone) via an optional Infisical key, e.g. `REVIEW_AI_CONTEXT`.
- Regenerate suggestion on demand (e.g. 🔄 reaction).
- Instant sends via Slack Events API (needs a server — breaks the no-server design; the twice-daily
  poll remains the cadence for approvals).
