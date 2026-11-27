## Maintaining This Repository

> [!CAUTION]
> ## Editing this repo? Never force-push to `main`
>
> 1. **State no longer lives in git — it lives on Cloudflare R2.** The workflows do
>    **not** commit state anymore, so a routine push will not race a state commit.
>    (Review text, `posted_ids`, reply status, and the RAG corpus are all on R2.)
> 2. **Never `git push --force` to `main`.** It rewrites shared history for everyone.
>    A rejected push is answered with *pull*, never *force*.
> 3. **If you changed any code, run all tests before pushing** — one command:
>    ```
>    python3 tests/run_all.py
>    ```
>    The workflow itself does not run tests, so this is the only safety check. Only push when it ends
>    with `OK`.

# Store Reviews — Centralised Review System

A single system that collects your app's customer reviews from the **Apple App Store** and **Google
Play**, drafts a suggested reply for each one, and shows them all on a **web dashboard**. A developer
reads each review, edits the suggested reply (or writes their own), and clicks **Reply** — the system
publishes that reply back to the store.

In short:

- **Reviews come in automatically** from the App Store and Google Play.
- Each review gets an **AI-suggested reply** — drafted only when our own past human replies give
  strong precedent (that is the **RAG** part; see [AI Reply Generation](#ai-reply-generation)).
- A **Slack message** tells the team when new reviews arrive, with a link to the dashboard.
- On the **dashboard**, a developer approves, edits, or rewrites the reply and sends it.
- An app can opt into **auto-reply**, in which case gated drafts are sent automatically.
- Once a reply is sent, that review **disappears from the dashboard**, and if a human wrote it, it is
  **folded back into the corpus** so future suggestions match the team's voice.

It runs entirely on **GitHub Actions** — there is **no server and no database** to maintain. All
durable text and state lives on **Cloudflare R2**. One central repository (`Lascade-Co/store-reviews`)
serves **any number of apps**.

> **This is the one document to read.** It covers onboarding, day-to-day use, both workflows, how RAG
> works, state management, and the architecture. Supporting references: `r2-path.md` (every R2 path),
> `docs/review-api-responses.md` (store API shapes), `CLAUDE.md`/`AGENTS.md` (conventions + sealed CI).

---

# Quick Start — Connect Your App

This checklist is all an experienced developer needs to connect a new app. Each step links to a
detailed explanation further down.

1. **Collect the required credentials** — Apple and/or Google API keys, plus your app's Infisical
   project slug and a chosen **appcode**. → See [What You Need](#what-you-need) and [Getting the Credentials](#getting-the-credentials--exact-steps).
2. **Choose a Slack channel** — use the shared `#store-reviews` channel (ID `C0C1190TA05`, the bot is
   already in it) or create your own and invite `@ReviewTravel`. → See [Step 1 — Slack Channel](#step-1--slack-channel).
3. **Create the `/reviews` folder in Infisical** — in your app's project, **Production** environment.
   → See [Step 2 — Infisical](#step-2--infisical).
4. **Add the required secrets** — the App Store / Google Play keys and `SLACK_CHANNEL_ID`. → See
   [Step 2 — Infisical](#step-2--infisical).
5. **Add the app to [`apps.json`](apps.json)** — one object with `appname`, `appcode`,
   `infisical_slug`, `type`, and `autoreply`. This is the actual onboarding. → See [Step 3 — Add the App to apps.json](#step-3--add-the-app-to-appsjson).
6. **Run the review sync manually** — `store-reviews` → **Actions → Review Sync (Central) → Run
   workflow**, with your `appcode` as `app_code`. → See [Step 4 — First Run and Verification](#step-4--first-run-and-verification).
7. **Verify** — the dashboard shows the app, and new reviews trigger a Slack notification.
   → See [Step 4 — First Run and Verification](#step-4--first-run-and-verification).
8. **Create a GitHub token** — needed only to *send* replies from the dashboard. → See
   [GitHub Token](#github-token).

Steps 1–5 are one-time setup. After the first run, the daily schedule handles everything.

---

# What You Need

| Requirement | Purpose |
|---|---|
| App Store Connect API key (`.p8`) with **Customer Reviews** read + response permission | Lets the system read and reply to App Store reviews. → [details](#app-store-credentials) |
| Numeric Apple **App ID** (not the bundle ID) | Identifies the iOS app on Apple's API. → [details](#app-store-credentials) |
| Google Play **service account JSON** with Play Console access to *view and reply to reviews* | Lets the system read and reply to Google Play reviews. → [details](#google-play-credentials) |
| The app's **Infisical project slug** | Fetches the app's secrets from Infisical (the `infisical_slug` in apps.json). → [details](#step-3--add-the-app-to-appsjson) |
| An **appcode** (short id you choose) | Names the R2 data file, the `?app=` dashboard link, and the state folder. → [details](#step-3--add-the-app-to-appsjson) |
| An **appname** (display name) | Shown on the dashboard and in the Slack notification. → [details](#step-3--add-the-app-to-appsjson) |
| A **type** (`flight`/`car`/`hotel`) | The category used by RAG retrieval to find similar past replies. → [details](#step-3--add-the-app-to-appsjson) |
| A Slack channel (shared or dedicated) | Where new-review notifications are posted. → [details](#step-1--slack-channel) |
| A fine-grained **GitHub token** (per developer) | Required only to send replies from the dashboard. → [details](#github-token) |

**Which platforms?** An app can be **iOS only**, **Android only**, or **both**. Only provide the
credentials for the platform(s) the app actually uses — a platform whose keys are absent is simply
skipped (its provider logs "not configured" and exits cleanly). You do **not** need a Slack bot token:
one shared bot posts for every app.

---

# How the System Works

```
App Store / Google Play
        │  reviews fetched automatically (once daily)
        ▼
  Review Sync  ──►  for each NEW review:  RAG retrieves similar past human replies
        │                                 gate passes → Codex drafts a reply
        │                                 gate fails  → no draft (goes to a human)
        │           review data + any draft written to storage (Cloudflare R2)
        │           if the app opted into autoreply → the gated draft is sent now
        ▼
  Slack notification  ──►  App Code / App Name / New Reviews: N / Review URL: <dashboard>/?app=<appcode>
        │
        ▼
  Web Dashboard  ──►  developer reviews, edits, or rewrites the reply → clicks Reply
        │
        ▼
  Reply Workflow  ──►  publishes the reply to the App Store / Google Play
                       marks the review replied (it leaves the dashboard)
                       a HUMAN reply is captured into the RAG corpus for next time
```

In plain terms:

- **Reviews are fetched automatically** on a daily schedule — no one has to trigger it.
- The system **drafts an AI reply** only when retrieval finds enough similar past **human** replies;
  otherwise the review is shown for a human to answer from scratch.
- **The AI reply is a suggestion** unless the app opted into **auto-reply**.
- **A developer decides what is sent** — accept the suggestion, edit it, or write their own.
- The **dashboard** is where reviews are read, edited, and sent.
- The **reply workflow** publishes the approved reply to the correct store.
- **Replied reviews leave the dashboard.** **Unreplied reviews stay for 7 days**, then expire.
- **Reviews are never shown twice** — each is deduplicated permanently (`posted_ids`).
- **Human replies teach the system** — they are added to the corpus so suggestions improve over time.

Detailed explanations of each stage:

- [Review Sync](#review-sync)
- [AI Reply Generation](#ai-reply-generation)
- [Dashboard](#dashboard)
- [Reply Workflow](#reply-workflow)
- [Review State and Deduplication](#review-state-and-deduplication)

---

# Day-to-Day Usage

Once an app is connected, the routine for handling a review is:

1. A **new review** is detected on the next daily sync.
2. A **Slack notification** is posted to the app's channel with a link to the dashboard.
3. The developer **opens the dashboard** from that link.
4. The developer **reads the AI-suggested reply** (if one was drafted).
5. The developer **edits it** if needed (or writes a new one).
6. The developer clicks **Reply** (or **Send All** to publish every drafted reply for the app at once).
7. The system **publishes the reply/replies** to the App Store or Google Play.
8. The replied reviews **disappear from the dashboard** once sent successfully, and any human-written
   reply is captured into the RAG corpus.

**Send All** sends every review that currently has a draft in a single workflow run — handy after
reviewing a batch. Each reply is still sent independently, so one failure doesn't block the others.

**Sending a reply needs a GitHub token.** The first time you click **Reply** or **Send All**, the
dashboard asks for a personal token, which is stored only in *your* browser. You create it once. →
See [GitHub Token](#github-token).

Two extra dashboard controls manage the learning corpus directly (see
[The RAG corpus](#the-rag-corpus-the-knowledge-base)):

- **Feed examples** — add review→reply example pairs to the corpus immediately (useful for seeding or
  testing before any real replies exist).
- **Clear corpus** — drop the whole corpus table (reset to a cold start).

---

# Review Behaviour

All current, documented behaviour of the system:

- **First run (baseline):** connecting an app shows **no existing reviews**. The first sync records
  every current review as a baseline and posts nothing — only reviews that arrive **after** the app
  is connected are ever shown.
- **Fetch frequency:** reviews are fetched **once daily** (see [Schedule](#schedule)).
- **Deduplication:** every review is shown **once**. A permanent record (`posted_ids`) ensures that
  even edited or re-appearing reviews never show again after they've been handled.
- **Unreplied reviews:** stay on the dashboard for **7 days**, then expire from the active set.
- **After replying:** the review is marked replied and removed from the dashboard (a replied thread is
  kept for a **2-day** edit window so a human can replace the reply).
- **Replies are suggestions by default:** a reply is published only when a developer clicks **Reply** —
  **unless** the app set `autoreply: true`, in which case a gated draft is sent automatically.
- **AI suggestion is gated:** a draft is produced only when RAG finds **≥ 5** similar past human
  replies above the similarity threshold; otherwise the review goes to a human (see
  [AI Reply Generation](#ai-reply-generation)).
- **AI suggestion language:** the suggested reply is generated in the **review's own language**.
- **Missing suggestion:** if a review is shown without a suggested reply (no precedent yet, or the AI
  call failed that run), it will **not** get one retroactively — write the reply manually. The sync
  itself is unaffected.
- **Google Play reply limit:** replies longer than **~350 characters** are truncated (store limit).
- **App Store reply limit:** longer replies are allowed.
- **Google Play fetch behaviour:** the Play API only returns reviews that have text, were
  created/modified in the **last 7 days**, on the **production track**.
- **App Store visibility:** published App Store responses can take a while to appear publicly.

---

# Connecting a New App — Detailed Explanation

This is the full version of the [Quick Start](#quick-start--connect-your-app).

## Prerequisites

Gather these before you start (see [What You Need](#what-you-need) for the summary and
[Getting the Credentials](#getting-the-credentials--exact-steps) for exact steps):

- **iOS:** App Store Connect API key (`.p8`), key ID, issuer ID, and the numeric Apple App ID.
- **Android:** Google Play service account JSON and the app's package name.
- **Both:** the app's Infisical project slug, a chosen appcode/appname/type, and a Slack channel.

Provide only the platform(s) the app uses. No Slack bot token is required — a shared bot posts for
all apps (optionally, an app can use its own bot by adding a non-empty `SLACK_BOT_TOKEN` to its
`/reviews` folder, which overrides the shared default).

## Step 1 — Slack Channel

Choose where this app's one-message-per-run notification lands:

- **Shared channel (easiest):** use `#store-reviews` — set `SLACK_CHANNEL_ID` to `C0C1190TA05`. The
  bot is already a member, so there is nothing to create.
- **Dedicated channel:** create the channel, run `/invite @ReviewTravel`, then copy its channel ID
  (channel name → View channel details → bottom of the About tab, starts with `C`). → See
  [Slack Channel ID](#slack-channel-id).

Use the chosen ID as `SLACK_CHANNEL_ID` in Step 2. The bot only **posts** to the channel — Slack is
notify-only; replies happen on the dashboard, not in Slack threads.

## Step 2 — Infisical

In the app's Infisical project on `secrets.lascade.com`, create a folder named **`reviews`** in the
**Production** environment (the workflow always reads Production), and add the secrets below.

Expected structure:

```text
Production
└── reviews
    ├── APPSTORE_API_KEY_ID                 # iOS
    ├── APPSTORE_API_PRIVATE_KEY            # iOS
    ├── APPSTORE_ISSUER_ID                  # iOS
    ├── APPSTORE_APPLE_ID                   # iOS
    ├── GOOGLE_PLAY_PACKAGE_NAME            # Android
    ├── GOOGLE_PLAY_SERVICE_ACCOUNT_JSON    # Android
    └── SLACK_CHANNEL_ID                    # both
```

| Secret | Value | Needed for |
|---|---|---|
| `APPSTORE_API_KEY_ID` | App Store Connect key ID | iOS |
| `APPSTORE_API_PRIVATE_KEY` | Full `.p8` file contents (multiline is fine) | iOS |
| `APPSTORE_ISSUER_ID` | Issuer UUID from the Integrations page | iOS |
| `APPSTORE_APPLE_ID` | Numeric Apple app ID | iOS |
| `GOOGLE_PLAY_PACKAGE_NAME` | e.g. `com.lascade.myapp` | Android |
| `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` | The service-account JSON, **base64-encoded** (`base64 -w0 key.json`) | Android |
| `SLACK_CHANNEL_ID` | The channel ID from [Step 1](#step-1--slack-channel) | both |

For an iOS-only or Android-only app, add only that platform's secrets plus `SLACK_CHANNEL_ID`.

<ins>For how to obtain each value, see [Getting the Credentials — exact steps](#getting-the-credentials--exact-steps).</ins>

## Step 3 — Add the App to apps.json

Add one object for the app to [`apps.json`](apps.json) (in this repo, on `main`):

```json
{
  "apps": [
    { "appname": "Flights70", "appcode": "D1AF", "infisical_slug": "flights70", "type": "flight", "autoreply": false },
    { "appname": "Hotels70",  "appcode": "D1AH", "infisical_slug": "hotels70",  "type": "hotel",  "autoreply": false },
    { "appname": "Your App",  "appcode": "your_app_code", "infisical_slug": "your-infisical-slug", "type": "flight", "autoreply": false }
  ]
}
```

That object is the entire onboarding — the scheduled workflow reads `apps.json` at runtime and picks
up the new app on its next run. No trigger workflow, no dispatch token, no code change. Each key:

- **`appcode`** — a short id **you choose**. It names the R2 data file (`<appcode>.json`), the
  `?app=<appcode>` dashboard link, and the state folder (`state/<appcode>/` on R2). Keep it stable once
  set. Use the platform code(s) as the appcode:

  - **Cross-platform (e.g. Flutter — one app on both stores):** the app has a **separate code per
    platform**, so join the two — `<android_code>_<ios_code>` — e.g. `D1AS_D1IS` (`D1AS` = Android,
    `D1IS` = iOS).

  - **Native single-platform (Android-only or iOS-only):** use that platform's single code directly,
    e.g. `D1AS` or `D1IS` — no underscore.

- **`infisical_slug`** — the app's **Infisical project slug**, used to fetch its `/reviews` secrets.
  It must match the Infisical Settings value **exactly**.

- **`appname`** — the display name shown on the dashboard and in the Slack notification.

- **`type`** — the app category (`flight`, `car`, or `hotel`). RAG retrieval only pulls example replies
  from the **same type**, so similar reviews across apps of the same category share precedent.

- **`autoreply`** — `false` (default) drafts the reply for a human to approve on the dashboard; `true`
  auto-sends any reply the RAG gate produced. Leave it `false` unless you explicitly want hands-off
  sending for that app.

To stop syncing an app, remove its object; its `state/<appcode>/` objects on R2 stay untouched.

## Step 4 — First Run and Verification

1. Run it manually without waiting for the cron: `store-reviews` → **Actions → Review Sync (Central)
   → Run workflow**, and set **app_code** to your app's `appcode` (leave it empty to sync every app in
   `apps.json`).
2. Watch the run in `store-reviews` → Actions. Expect:
   - **Fetch review secrets from Infisical** turns the `/reviews` keys into environment variables.
   - **Sync Reviews and Publish Dashboard Data** runs. On this **first (baseline) run it records
     existing reviews as seen and publishes none** — later runs pick up only reviews that arrive after
     connection, generate suggestions, and upload `<appcode>.json` to R2.
   - State is **written to R2** under `<R2_DATA_PREFIX>/state/<appcode>/` automatically (no git commit).
     No Slack message is sent on the baseline run, since no new reviews are published.
3. Confirm the run succeeded. The dashboard for a freshly connected app shows **no reviews** until new
   ones arrive — that is expected. When the first new review comes in, the next sync posts a Slack
   notification and it appears on the dashboard, where you click **Reply** (the first time asks for a
   token, see [GitHub Token](#github-token)).

From now on the schedule handles everything; no manual state setup is ever needed.

---

# Getting the Credentials — exact steps

## App Store Credentials

[APPSTORE_APPLE_ID](https://appstoreconnect.apple.com)
`(Identifies the app on Apple's API — the numeric Apple ID like 6443538575, NOT the bundle ID)`

1. Open App Store Connect (appstoreconnect.apple.com) → Apps → select the app.
2. Left sidebar → App Information.
3. Under General Information, copy the number in the Apple ID field.
4. Shortcut for released apps: open the app's App Store page and copy the digits after "id" in the URL (.../app/myapp/id6443538575).


[APPSTORE_API_KEY_ID](https://appstoreconnect.apple.com/access/integrations/api), [APPSTORE_ISSUER_ID](https://appstoreconnect.apple.com/access/integrations/api), [APPSTORE_API_PRIVATE_KEY](https://appstoreconnect.apple.com/access/integrations/api)
`(Lets the bot log in to Apple to read reviews and publish responses)`

1. Open App Store Connect → Users and Access → Integrations → App Store Connect API → Team Keys → the plus (+) button.
2. Name the key (e.g. review-bot), role App Manager, then Generate.
3. Copy Issuer ID (shown at the top of the page) → APPSTORE_ISSUER_ID.
4. Copy the key's Key ID → APPSTORE_API_KEY_ID.
5. Download the .p8 file (Apple allows this only once — keep it safe). Paste its entire contents, including the BEGIN/END lines → APPSTORE_API_PRIVATE_KEY.

## Google Play Credentials

[GOOGLE_PLAY_SERVICE_ACCOUNT_JSON](https://console.cloud.google.com)
`(Lets the bot log in to Google Play — the JSON key is the bot's identity; its permissions are granted separately in Play Console)`

1. Open Google Cloud Console (console.cloud.google.com) → create/select a project.
2. APIs & Services → Library → search "Google Play Android Developer API" → Enable.
3. IAM & Admin → Service Accounts → Create service account → name it (e.g. review-monitor) → skip the role screens (no GCP roles needed) → Done.
4. Open the created account → Keys → Add key → Create new key → JSON → a .json file downloads.
5. Open Play Console (play.google.com/console) → Users and permissions → Invite new users → paste the service-account email (review-monitor@PROJECT.iam.gserviceaccount.com).
6. Open the App permissions tab → Add app → select the app. Skipping this step is the #1 mistake — every API call then fails with 403 PERMISSION_DENIED.
7. On that app tick BOTH permissions: "View app information (read-only)" and "Reply to reviews".
8. Send invite. Google may take up to 24 hours to activate the access — if a run still fails with 403, wait and retry (making any trivial edit in Play Console and saving speeds it up).
9. Base64-encode the downloaded file and paste that single line → GOOGLE_PLAY_SERVICE_ACCOUNT_JSON. Use `base64 -w0 key.json` (Linux) or `base64 -i key.json` (macOS). Encode the **whole** file — don't trim any fields. (Base64 keeps it one line and avoids any newline mangling of the private key; raw JSON is not accepted.)
10. Permission changes later never require regenerating this JSON — Google checks permissions fresh on every call.


[GOOGLE_PLAY_PACKAGE_NAME](https://play.google.com/console)
`(Identifies the app on Google's API — the package name like com.lascade.myapp)`

1. Open Play Console → select the app — the package name is shown under the app name (or copy it from the app's Play Store URL after "id=").
2. The app must belong to this Play account and have a production release (a wrong package also returns 403, not 404).

## Slack Channel ID

[SLACK_CHANNEL_ID](https://app.slack.com)
`(Tells the bot which Slack channel receives this app's per-run notification)`

1. In Slack, open the channel → click the channel name → View channel details.
2. Scroll to the bottom of the About tab and copy the Channel ID (starts with C).

The shared `#store-reviews` channel ID is `C0C1190TA05`.

## GitHub Token

Sending a reply runs the reply workflow, which needs a token stored in *your* browser only. Create a
**fine-grained personal access token**:

1. GitHub → Settings → Developer settings → Personal access tokens → **Fine-grained tokens** →
   Generate new token.
2. **Resource owner:** `Lascade-Co`. **Repository access:** Only select repositories →
   `store-reviews`.
3. **Repository permissions:** **Actions → Read and write** (nothing else; Metadata: read is added
   automatically).
4. Generate, copy the `github_pat_…` value, and paste it into the dashboard popup. The dashboard
   verifies it (real, not expired, can access the repo) before saving it to your browser's
   localStorage.

If the org requires approval for fine-grained tokens, the token stays "pending" until an org admin
approves it in GitHub — a reply will fail until then.

---

# Technical Details

## Review Sync

Workflow 1 — `.github/workflows/review-sync.yml` (**Review Sync (Central)**). One scheduled run
fans out over every app in [`apps.json`](apps.json) and processes each as a parallel matrix job
(`fail-fast: false`, so one app failing never stops the others). The jobs:

- **`set-matrix`** reads `apps.json` → emits one matrix entry per app (or just the one `app_code` for a
  manual single-app run).
- **`sync`** (one job per app, `environment: production`) runs `scripts/main.py`, which for the app:
  1. Loads state from R2 for both providers (`state/<appcode>/appstore.json` + `playstore.json`).
  2. Fetches the newest reviews from each configured platform (App Store via a short-lived JWT; Google
     Play via an OAuth token). On the app's **first (baseline) run** it records all existing reviews as
     already-seen and posts nothing; every later run picks up only reviews that arrived since.
  3. Selects genuinely **new** reviews (not already in `posted_ids`/`reviews`).
  4. For each new review, runs **RAG retrieval**; reviews that clear the gate get a **Codex** draft
     (see [AI Reply Generation](#ai-reply-generation)).
  5. If the app set `autoreply: true`, sends any gated draft to the store immediately (marked
     `auto_replied`).
  6. Uploads the app's pending-review file `<appcode>.json` to Cloudflare R2.
  7. Posts one Slack notification to the app's channel (notify-only, with the dashboard link).
  8. Prunes inactive reviews and saves state back to R2.
- **`consolidate-corpus`** (one job for the whole run, after all `sync` jobs) folds the human replies
  captured during reply runs into the RAG corpus — it is the **only** writer of the vector table (see
  [The learning loop](#the-learning-loop)).

A platform whose keys are absent from `/reviews` is skipped (logs "not configured"). Runs can also be
triggered manually via **Run workflow** — with an `app_code` for one app, or empty for all. See
[Schedule](#schedule).

## AI Reply Generation

Suggested replies use **RAG (Retrieval-Augmented Generation)** on top of the **Codex** CLI. The point
of RAG here is to answer one question before drafting: *"Have our humans replied to a review like this
before, and in what voice?"* The vectors only **pick examples**; the language model receives plain
text, never the database.

### The RAG corpus (the knowledge base)

- A single **LanceDB** table named `replies`, stored on the private R2 bucket at
  `s3://<bucket>/corpus/lancedb/`. LanceDB is an embedded, S3-compatible vector store — **not**
  Cloudflare Vectorize (a hosted product we deliberately avoid).
- One row per past **human** review→reply pair, with its embedding and metadata (`app`, `type`,
  `store`, `rating`, `lang`, …). The columns are listed in `r2-path.md`.
- Rows arrive two ways: the dashboard **Feed examples** action (manual seeding), and the
  **consolidation** job draining pending files written whenever a human sends a reply.

### Embeddings (how "similar" is measured)

- `scripts/common/embeddings.py` uses **BGE-M3** (multilingual, 1024-dimensional) via **fastembed**
  (ONNX, CPU — no torch, which suits a short CI job). Model weights are cached across runs.
- The embedding **model**, not the vector store, is what makes retrieval work across languages: a
  review in any language maps to a vector where similar meanings land near each other.

### The retrieval gate

For each new review (`scripts/common/rag.py`):

1. Short-circuits (no draft) if the review is empty or the corpus is empty (cold start).
2. Embeds the review and searches for nearest past replies **in the same rating band** (`neg` ≤2,
   `mid` =3, `pos` ≥4) **and same `type`**, preferring the **same app**.
3. **Gate:** returns examples only when **≥ K** of them clear the similarity **threshold** — defaults
   `K = 5` (`RAG_MIN_EXAMPLES`) and `threshold = 0.90` cosine (`RAG_SIMILARITY_THRESHOLD`). Below the
   bar → no examples → **no AI draft**, and the review goes to a human (which then seeds the corpus).

This strictness avoids a **cold-start echo chamber** and keeps every generated reply anchored to real
human precedent. Retrieval is always optional — any failure logs and proceeds without examples.

### Generation (Codex)

- Only reviews that **passed the gate** carry their retrieved `examples` and are sent to **Codex** (the
  `@openai/codex` CLI, authenticated by the org ChatGPT plan via `CODEX_AUTH_JSON_BASE_64` — no
  per-token OpenAI key or billing).
- **All of a run's gated reviews go in ONE Codex invocation per provider**; it writes a
  `{review_id: reply}` JSON file the sync reads.
- The prompt: match the voice of the attached human examples, reply in the review's language, be warm
  and concise, **never** promise refunds/compensation or invent facts, redirect booking/payment issues
  to the travel provider, and stay ≤340 characters.
- **Failure-safe:** missing auth, a Codex error, a timeout, or bad output all log a warning and the
  review is published **without** a suggestion. The sync never fails because the AI is unavailable.

### Send decision

Whether a draft is **sent automatically** is decided *after* generation by the per-app **`autoreply`**
flag — not by the model. `autoreply: true` auto-sends the gated draft; `false` shows it on the
dashboard for a human.

### The learning loop

When a human writes or edits a reply and sends it from the dashboard, `post_reply.py` drops a
**pending file** on R2 (`corpus/pending/<appcode>__<store>__<reviewId>.json`). The next sync's
`consolidate-corpus` job embeds it and upserts it into the LanceDB table, then deletes the pending
file. **Only human replies seed the corpus** (AI-accepted or auto-sent ones never do) — the
echo-chamber rule — so the corpus only ever grows from real human writing.

## Dashboard

The web dashboard lives in **`dashboard/`** (Vite + React, deployed to **Cloudflare Pages**
through the central `Lascade-Co/actions` repository). It reads each app's pending-review file
(`<appcode>.json`) from Cloudflare R2's public domain and renders the reviews with their suggested
replies (showing the app's `appname` and `appcode`). A developer edits or rewrites a reply and clicks
**Reply**, which triggers the [Reply Workflow](#reply-workflow) via the GitHub API using their
browser-stored [GitHub token](#github-token) — passing the `appcode`, which the reply workflow maps
back to the `infisical_slug` via apps.json. The dashboard link used in Slack comes from the
`SITE_BASE_URL` secret, and the app is selected by the `?app=<appcode>` query parameter.

Because the dashboard fetches its data in the browser, the R2 bucket must allow cross-origin GET
requests from the dashboard's origin (a **CORS policy**).

## Reply Workflow

Workflow 2 — `.github/workflows/reply-review.yml`. Dispatched from the dashboard. It has **one entry
point and three actions**, each routed to its own job (least-privilege: only `reply` gets store
credentials; `feed`/`clear` are R2-only):

- **`reply`** — publish a dashboard-approved reply. **Single** mode: `platform`, `review_id`,
  `reply_text`. **Batch** (`Send All`) mode: `replies` = a JSON array of
  `{platform, review_id, reply_text, is_human}`. The job (`scripts/post_reply.py`) sends each reply to
  the store (create-or-replace, idempotent), marks the review replied in state, rebuilds the app's R2
  file so replied reviews leave the dashboard, and — for any reply the operator **wrote or edited**
  (`is_human`) — captures a corpus pending file. Each reply in a batch is isolated (one failure never
  blocks the rest).
- **`feed`** — embed `{review, reply, rating}` example pairs and upsert them into the corpus
  immediately (seed/test data). R2 only, no store credentials.
- **`clear`** — drop the whole corpus table (reset to cold start). R2 only.

`feed` and `clear` share the **`corpus-consolidate`** concurrency group with the sync's consolidation
job, so the LanceDB table never has two writers at once. The `reply` action uses a **per-review**
concurrency group (`reply-<appcode>-<review_id>`), so replies to different reviews — even of the same
app — run in parallel and never cancel each other; only a double-click on the *same* review is
de-duplicated. Concurrent state writes are reconciled on R2 (see
[Review State and Deduplication](#review-state-and-deduplication)).

This workflow must exist on the default branch (`main`), and the developer's token must have
**Actions: Read and write** on `store-reviews` (see [GitHub Token](#github-token)).

## Review State and Deduplication

State is the system's **operational memory** — what has been seen and done. It exists because the
store APIs re-hand the current window of reviews on every call and never say "here is what changed
since last time," so we must remember that ourselves. It is **not** a by-product of using GitHub
Actions.

**Where it lives.** One JSON object per app + provider on Cloudflare R2:

```
<R2_DATA_PREFIX>/state/<appcode>/appstore.json
<R2_DATA_PREFIX>/state/<appcode>/playstore.json
```

Created lazily (an Android-only app only ever writes `playstore.json`). The review *text* lives in the
app's dashboard file; state stores ids, flags, and hashes — no review text.

**Key fields.**

- **`baselined`** — set on the app's first run: every review that existed at connection is recorded in
  `posted_ids` and nothing is posted, so only later reviews are ever shown. Set even when the app had
  zero reviews at connection, so the first real review is never missed.
- **`posted_ids`** — the **permanent deduplication record**, newest-first, **never pruned**. A review
  whose id is here is never shown again, even if later edited or re-appearing.
- **`reviews`** — only the **active** reviews still worth tracking for replies. Old ones are pruned
  from here (their ids stay in `posted_ids`). Pruning windows: **7 days** for un-replied reviews
  (`OPEN_POLL_WINDOW_DAYS`), **2 days** after a reply is sent (`REPLY_EDIT_WINDOW_DAYS`, the edit
  window).

**Concurrency (safe without a database).** A sync run and a reply run can both write the same app's
object. Writes use **R2 conditional puts**: `If-None-Match: *` to create, `If-Match: <etag>` to update.
On a **412** (someone wrote in between) the writer re-reads, merges (`merge_state.py` — unions
`posted_ids`, keeps the newest reply per review, preserves "reply sent" flags), and retries. A per-app
concurrency group shared between the two workflows means a sync and a reply for the **same** app never
run at once; different apps run in parallel. This replaced the old git-commit-state job; the merge and
prune logic is reused as-is — only the transport changed from a git push to an R2 conditional put.

## "Do we still need state and posted_ids?" — the architecture question

A common question: *"If we move to a real server instead of this workflow, isn't state management
unneeded? And aren't `posted_ids` unneeded now that we use a dashboard instead of the Slack
notification?"* **No to both** — but the reasons are worth understanding:

- **A server does not remove state — it relocates it.** State exists because of the **store APIs**, not
  GitHub Actions. A long-running server would store the same "what's new / what's replied" memory in a
  database row instead of an R2 JSON object, and handle concurrency with a DB transaction instead of an
  ETag conditional put. The *mechanism* changes; the *need* does not.
- **`posted_ids` is dedup, unrelated to Slack.** It prevents re-processing the same review. Slack vs.
  dashboard is only the notification/approval surface. Without a "seen" record, every run would
  re-collect the same reviews as new, re-draft them, possibly re-auto-reply, and the dashboard would
  re-show already-answered reviews (inviting duplicate store replies). The one accurate half of the
  intuition: `posted_ids` is a compact, prune-surviving id *list* **because** state is a small JSON
  file; a database might instead keep every review row and query `WHERE status <> 'seen'`. Same
  concept, different shape.
- **When a server/DB is actually worth it:** not for correctness (the current design is correct), but
  when GitHub Actions operational limits bite — per-run setup time, store/Slack rate limits at higher
  app counts, or sub-daily latency. The evolution would then be **R2-JSON state → database-backed state
  → queue + worker service**, carrying the same dedup/boundary/reply model across.

## Schedule

One central cron at **~02:00 IST** (`cron: "30 20 * * *"` in UTC — 02:00 IST is 20:30 UTC) runs every
app in `apps.json` in parallel (one matrix job per app). Manual runs are available anytime via **Run
workflow** (set `app_code` for one app, or leave it empty for all). GitHub may delay a scheduled run by
a few minutes. A new review appears on the dashboard after the next run (scheduled or manual); replies
you send are published within about a minute, independent of the schedule.

---

# Reference

## Secrets and environments

All pipeline jobs that use secrets declare **`environment: production`** and read from the production
environment (repository-level secrets are empty by design).

| Secret (central repo, production env) | Purpose |
|---|---|
| `INFISICAL_CLIENT_ID` / `INFISICAL_CLIENT_SECRET` / `INFISICAL_DOMAIN` | Fetch each app's `/reviews` secrets from Infisical |
| `SLACK_BOT_TOKEN` | Shared bot that posts the per-run notification |
| `CODEX_AUTH_JSON_BASE_64` | Codex auth (org ChatGPT plan) for AI-suggested replies |
| `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_BUCKET` / `R2_DATA_PREFIX` | Dashboard data files, state, and the corpus on R2 |
| `SITE_BASE_URL` | Dashboard base URL used in the Slack notification link |

- **Per-app store credentials are not GitHub secrets** — they are fetched at runtime from the app's
  **Infisical** `/reviews` folder (`APPSTORE_*`, `GOOGLE_PLAY_*`, optional per-app `SLACK_BOT_TOKEN`).
- `R2_CORPUS_PREFIX` (default `corpus`) optionally relocates the corpus root.
- `dashboard-deploy.yml` is a reusable-workflow caller and cannot carry `environment: production`, so
  its `CI_APP_*` secrets must be **repository-level**.
- The only browser-side credential is each developer's fine-grained token for the **Reply** button.

CI output is **sealed** (encrypted to the repo key) because the repo is public — see the sealed-output
rules in `CLAUDE.md`, and read runs at `https://ci.lascade.com/?repo=store-reviews`.

## R2 storage layout (summary)

| What | R2 path | Written by | Read by |
|---|---|---|---|
| Dashboard data file | `<DATA_PREFIX>/<appcode>.json` | sync + reply | dashboard, reply workflow |
| Review state | `<DATA_PREFIX>/state/<appcode>/<provider>.json` | state_manager (ETag CP) | sync + reply |
| Corpus pending | `<CORPUS_PREFIX>/pending/<appcode>__<store>__<id>.json` | reply (human) | consolidation → deleted |
| Corpus (LanceDB) | `<CORPUS_PREFIX>/lancedb/` table `replies` | consolidation + feed | RAG retrieval |

Full detail, including the LanceDB columns and the public read domain, is in **`r2-path.md`**.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Error: Missing universal auth credentials` in the Infisical step | The `INFISICAL_CLIENT_ID/SECRET/DOMAIN` secrets are missing/empty in the production environment. Ask the backend team to (re-)provision them. |
| Infisical step fails with project not found | The `infisical_slug` in `apps.json` doesn't match the Infisical **project slug** (the Settings value, not the display name). |
| A provider logs "not configured for this app; skipping" | That platform's keys are absent from `/reviews`. Intentional for single-platform apps; otherwise add the missing keys. |
| Google Play returns 0 reviews | Normal: the Play API only returns reviews that have text from the last 7 days, production track only. |
| A new app never runs | Its object isn't in `apps.json`, or the JSON is malformed (the **Resolve app list** job logs the parsed apps). |
| Reply run fails with "No app in apps.json has appcode …" | The dashboard's `appcode` has no matching object in `apps.json` on `main`. Add it or fix the appcode. |
| Dashboard shows no reviews / a CORS error in the console | The R2 bucket needs a CORS policy allowing GET from the dashboard's origin, OR the sync hasn't run yet for this app, OR the `?app=` appcode is wrong. The console `[reviews]` logs pinpoint which. |
| Review shown without a Suggested Reply | No qualifying precedent yet (RAG gate closed), `CODEX_AUTH_JSON_BASE_64` missing/stale, or the Codex call failed that run — the sync is unaffected. It won't get a suggestion later; write the reply manually. |
| Reply button → "token rejected" | The token is expired, lacks Actions: Read and write on `store-reviews`, or is pending org approval. |
| Reply button → 404 | `reply-review.yml` must be on `main`, and the token's repository access must include `store-reviews`. |
| No Slack notification but the dashboard has data | `SLACK_BOT_TOKEN` missing from the production environment, wrong `SLACK_CHANNEL_ID`, bot not in the channel, or `SITE_BASE_URL` unset (data still publishes). |

## Repository Layout

```
.github/workflows/review-sync.yml    Workflow 1: cron fans out over apps.json, syncs each app, drafts replies (RAG+Codex), publishes to R2, consolidates the corpus
.github/workflows/reply-review.yml   Workflow 2: dashboard-dispatched reply | feed | clear
.github/workflows/dashboard-deploy.yml  Deploys dashboard/ to Cloudflare Pages
apps.json                            The app list — one object per app { appname, appcode, infisical_slug, type, autoreply }
dashboard/                           Web dashboard (Vite + React; deployed to Cloudflare Pages)
scripts/                             Python sync + reply logic
  main.py                              sync entry point (both providers, one process per app)
  post_reply.py                        reply entry point (send to store + capture human reply)
  corpus_action.py                     feed / clear corpus entry point
  consolidate_corpus.py                fold pending human replies into the LanceDB corpus
  providers/                           appstore.py, playstore.py (fetch + reply)
  common/                              state_manager, rag, embeddings, corpus, ai_reply, publish, merge_state, prune_state, review_sync, slack_client, jwt_generator
state/<appcode>/                     Legacy git state (no longer written; state now lives on R2)
tests/                               Unit tests (run locally via tests/run_all.py before pushing)
r2-path.md                           Full R2 path + corpus-column reference
docs/review-api-responses.md         App Store + Google Play API response shapes
```
</content>
