


The **web dashboard** is a separate project (Vite + React, deployed to a Cloudflare Worker); it is
not in this repo.

## Maintaining This Repository

> [!CAUTION]
> ## Editing this repo? Always pull before pushing — NEVER force-push
>
> 1. **Pull right before you push:** `git pull --rebase origin main`. A "non-fast-forward" rejection
>    just means a workflow committed state in between — pull and push again; nothing is broken.
> 2. **Never `git push --force` to `main`.** It rewrites history and **deletes the state commits made
>    since your last pull** — the permanent dedup ids (`posted_ids`) and reply status. The next run
>    would then re-publish already-seen reviews and lose track of pending replies. A rejected push is
>    always answered with *pull*, never *force*.
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
- Each review gets an **AI-suggested reply** — a starting point, never sent on its own.
- A **Slack message** tells the team when new reviews arrive, with a link to the dashboard.
- On the **dashboard**, a developer approves, edits, or rewrites the reply and sends it.
- Once a reply is sent, that review **disappears from the dashboard**.

It runs entirely on **GitHub Actions** — there is no server and no database to maintain. One central
repository (`Lascade-Co/store-reviews`) serves **any number of apps**.

---

# Quick Start — Connect Your App

This checklist is all an experienced developer needs to connect a new app. Each step links to a
detailed explanation further down.

1. **Collect the required credentials** — Apple and/or Google API keys, plus your app's Infisical
   project slug. → See [What You Need](#what-you-need) and [Getting the Credentials](#getting-the-credentials--exact-steps).
2. **Choose a Slack channel** — use the shared `#store-reviews` channel (ID `C0C1190TA05`, the bot is
   already in it) or create your own and invite `@ReviewTravel`. → See [Step 1 — Slack Channel](#step-1--slack-channel).
3. **Create the `/reviews` folder in Infisical** — in your app's project, **Production** environment.
   → See [Step 2 — Infisical](#step-2--infisical).
4. **Add the required secrets** — the App Store / Google Play keys and `SLACK_CHANNEL_ID`. → See
   [Step 2 — Infisical](#step-2--infisical).
5. **Add the app's Infisical project slug to [`apps.json`](apps.json)** — this one line is the actual
   onboarding. → See [Step 3 — Add the App to apps.json](#step-3--add-the-app-to-appsjson).
6. **Run the review sync manually** — `store-reviews` → **Actions → Review Sync (Central) → Run
   workflow**, with your slug as `project_slug`. → See [Step 4 — First Run and Verification](#step-4--first-run-and-verification).
7. **Verify** — the Slack channel gets a notification and the dashboard shows the app's reviews.
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
| The app's **Infisical project slug** | Selects the app's secrets, names its state folder, and is the `?app=` value in the dashboard link. → [details](#step-3--add-the-app-to-appsjson) |
| A Slack channel (shared or dedicated) | Where new-review notifications are posted. → [details](#step-1--slack-channel) |
| A fine-grained **GitHub token** (per developer) | Required only to send replies from the dashboard. → [details](#github-token) |

**Which platforms?** An app can be **iOS only**, **Android only**, or **both**. Only provide the
credentials for the platform(s) the app actually uses — a platform whose keys are absent is simply
skipped (its job logs "not configured" and exits cleanly). You do **not** need a Slack bot token: one
shared bot posts for every app.

---

# How the System Works

```
App Store / Google Play
        │  reviews fetched automatically (once daily)
        ▼
  Review Sync  ──────────────►  AI suggestion (Codex) drafted for each review
        │                       review data written to storage (Cloudflare R2)
        ▼
  Slack notification  ──►  "N new reviews — <dashboard>/?app=<slug>"
        │
        ▼
  Web Dashboard  ──►  developer reviews, edits, or rewrites the reply → clicks Reply
        │
        ▼
  Reply Workflow  ──►  publishes the reply to the App Store / Google Play
                       the review is marked replied and leaves the dashboard
```

In plain terms:

- **Reviews are fetched automatically** on a daily schedule — no one has to trigger it.
- The system **drafts an AI reply** for each new review in the review's own language.
- **The AI reply is only a suggestion.** Nothing is ever sent automatically.
- **A developer decides what is actually sent** — they can accept the suggestion, edit it, or write
  their own.
- The **dashboard** is where reviews are read, edited, and sent.
- The **reply workflow** publishes the approved reply to the correct store.
- **Replied reviews leave the dashboard.** **Unreplied reviews stay for 7 days**, then expire.
- **Reviews are never shown twice** — each is deduplicated permanently.

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
4. The developer **reads the AI-suggested reply**.
5. The developer **edits it** if needed (or writes a new one).
6. The developer clicks **Reply**.
7. The system **publishes the reply** to the App Store or Google Play.
8. The review **disappears from the dashboard** once the reply is sent successfully.

**Sending a reply needs a GitHub token.** The first time you click **Reply**, the dashboard asks for
a personal token, which is stored only in *your* browser. You create it once. → See
[GitHub Token](#github-token).

---

# Review Behaviour

All current, documented behaviour of the system:

- **Fetch frequency:** reviews are fetched **once daily** (see [Schedule](#schedule)).
- **Deduplication:** every review is shown **once**. A permanent record (`posted_ids`) ensures that
  even edited or re-appearing reviews never show again after they've been handled.
- **Unreplied reviews:** stay on the dashboard for **7 days**, then expire.
- **After replying:** the review is marked replied and removed from the dashboard.
- **Replies are never automatic:** the AI reply is only a suggestion; a reply is published **only**
  when a developer clicks **Reply**.
- **AI suggestion language:** the suggested reply is generated in the **review's own language**.
- **Missing suggestion:** if a review is shown without a suggested reply (the AI call failed that
  run), it will **not** get one later — write the reply manually. The sync itself is unaffected.
- **Google Play reply limit:** replies longer than **350 characters** are truncated (store limit).
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
- **Both:** the app's Infisical project slug and a Slack channel.

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

Use the chosen ID as `SLACK_CHANNEL_ID` in Step 2. The bot only **posts** to the channel — it does
not read threads.

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
| `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` | Raw service-account JSON (**not** base64) | Android |
| `SLACK_CHANNEL_ID` | The channel ID from [Step 1](#step-1--slack-channel) | both |

For an iOS-only or Android-only app, add only that platform's secrets plus `SLACK_CHANNEL_ID`.

<ins>For how to obtain each value, see [Getting the Credentials — exact steps](#getting-the-credentials--exact-steps).</ins>

## Step 3 — Add the App to apps.json

Add the app's **Infisical project slug** to [`apps.json`](apps.json) (in this repo, on `main`):

```json
{
  "slugs": [
    "airlines70",
    "flight_deals",
    "your-infisical-project-slug"
  ]
}
```

This one line is the entire onboarding — the scheduled workflow reads `apps.json` at runtime and
picks up the new slug on its next run. No trigger workflow, no dispatch token, no code change.

- The slug selects the app's `/reviews` secrets, names its state folder (`state/<slug>/`), and is the
  `?app=` value in the dashboard link — it must match the Infisical project slug **exactly**.
- To stop syncing an app, remove its slug from the list; its `state/<slug>/` folder stays untouched.

## Step 4 — First Run and Verification

1. Run it manually without waiting for the cron: `store-reviews` → **Actions → Review Sync (Central)
   → Run workflow**, and set **project_slug** to your app's slug (leave it empty to sync every app in
   `apps.json`).
2. Watch the run in `store-reviews` → Actions. Expect:
   - **Fetch review secrets from Infisical** turns the `/reviews` keys into environment variables.
   - **Sync Reviews and Publish Dashboard Data** fetches the newest reviews (up to 5 per platform on
     the first run), generates suggestions, and uploads `<project_slug>.json` to R2.
   - The Slack channel receives one message with the dashboard link.
   - **Commit Updated State** creates and pushes `state/<project_slug>/` automatically.
3. Open the dashboard link (`<dashboard>/?app=<project_slug>`) — the reviews render with their
   suggested replies. Click **Reply** on one (the first time asks for a token, see
   [GitHub Token](#github-token)). Confirm the reply appears on the store.

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
9. Paste the downloaded file's entire raw contents (not base64) → GOOGLE_PLAY_SERVICE_ACCOUNT_JSON.
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
fans out over every slug in [`apps.json`](apps.json) and processes each app as a parallel matrix job.
For each app it:

1. Fetches the app's `/reviews` secrets from Infisical (Production).
2. Fetches the newest reviews from each configured platform (up to 5 per platform on the first run).
3. Generates an AI suggestion for each new review (see [AI Reply Generation](#ai-reply-generation)).
4. Uploads the app's pending-review file `<project_slug>.json` to Cloudflare R2.
5. Posts one Slack notification to the app's channel.
6. Commits the updated state to `state/<project_slug>/`.

A platform whose keys are absent from `/reviews` is skipped (logs "not configured"). Runs can also be
triggered manually via **Run workflow** — with a `project_slug` for one app, or empty for all. See
[Schedule](#schedule).

## AI Reply Generation

Suggested replies are generated by the **Codex** CLI, authenticated by the org ChatGPT plan through
the `CODEX_AUTH_JSON_BASE_64` secret (not a per-token OpenAI key). Each suggestion is drafted in the
**review's own language** and is only a **suggestion** — it is never published until a developer
clicks **Reply**. Google Play replies over **350 characters** are truncated to fit the store limit;
the App Store allows longer replies. If the Codex call fails for a run, affected reviews appear
without a suggestion and will not receive one later — the reply can still be written manually, and the
sync itself is unaffected.

## Dashboard

The web dashboard is a **separate project** (Vite + React, deployed to a **Cloudflare Worker**); it
is not in this repository. It reads each app's pending-review file (`<project_slug>.json`) from
Cloudflare R2 and renders the reviews with their suggested replies. A developer edits or rewrites a
reply and clicks **Reply**, which triggers the [Reply Workflow](#reply-workflow) via the GitHub API
using their browser-stored [GitHub token](#github-token). The dashboard link used in Slack comes from
the `SITE_BASE_URL` secret, and the app is selected by the `?app=<project_slug>` query parameter.

Because the dashboard fetches its data in the browser, the R2 bucket must allow cross-origin GET
requests from the dashboard's origin (a **CORS policy**).

## Reply Workflow

Workflow 2 — `.github/workflows/reply-review.yml`. Triggered from the dashboard when a developer
clicks **Reply**. It publishes the approved reply text to the correct store (App Store or Google
Play), marks the review as replied in state, and rebuilds the app's R2 file so the review leaves the
dashboard. This workflow must exist on the default branch (`main`), and the developer's token must
have **Actions: Read and write** on `store-reviews` (see [GitHub Token](#github-token)).

## Review State and Deduplication

Per-app state is committed to this repo under `state/<project_slug>/`; the review content itself lives
in the per-app JSON file on Cloudflare R2 (git state deliberately stores no review text).

- **`posted_ids`** is the permanent deduplication record — a review whose id is in `posted_ids` is
  never shown again, even if it is later edited or re-appears.
- **Unreplied reviews** remain on the dashboard for **7 days**, then expire.
- **Replied reviews** are removed from the dashboard.
- Because every run commits state to `main`, the branch moves even when no human is working. The
  workflow reconciles concurrent runs automatically; when **you** edit the repo, follow the rules in
  [Maintaining This Repository](#maintaining-this-repository).

## Schedule

One central cron at **~06:00 IST** (`cron: "30 0 * * *"` in UTC) runs every app in `apps.json` in
parallel (one matrix job per app). Manual runs are available anytime via **Run workflow** (set
`project_slug` for one app, or leave it empty for all). GitHub may delay a scheduled run by a few
minutes. A new review appears on the dashboard after the next run (scheduled or manual); replies you
send are published within about a minute, independent of the schedule.

---

# Reference

## GitHub Secrets — central repo (`Lascade-Co/store-reviews`)

| Secret | Purpose |
|---|---|
| `INFISICAL_CLIENT_ID` / `INFISICAL_CLIENT_SECRET` / `INFISICAL_DOMAIN` | Fetch each app's `/reviews` secrets from Infisical |
| `SLACK_BOT_TOKEN` | Shared bot that posts the per-run notification |
| `CODEX_AUTH_JSON_BASE_64` | Codex auth (org ChatGPT plan) for AI-suggested replies |
| `R2_ACCOUNT_ID` / `R2_ACCESS_KEY_ID` / `R2_SECRET_ACCESS_KEY` / `R2_BUCKET` / `R2_DATA_PREFIX` | Upload the per-app data file to Cloudflare R2 |
| `SITE_BASE_URL` | Dashboard base URL used in the Slack notification link |

All secrets live on this central repo — there is no per-app dispatch token (the app list is just
`apps.json`). The only browser-side credential is each developer's fine-grained token for the
**Reply** button.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Error: Missing universal auth credentials` in the Infisical step | The `INFISICAL_CLIENT_ID/SECRET/DOMAIN` secrets are missing/empty on `store-reviews`. Ask the backend team to (re-)provision them. |
| Infisical step fails with project not found | The slug in `apps.json` (or the manual `project_slug` input) doesn't match the Infisical **project slug** (the Settings value, not the display name). |
| A provider logs "not configured for this app; skipping" | That platform's keys are absent from `/reviews`. Intentional for single-platform apps; otherwise add the missing keys. |
| Google Play returns 0 reviews | Normal: the Play API only returns reviews that have text from the last 7 days, production track only. |
| A new app never runs | Its slug isn't in `apps.json`, or the JSON is malformed (the **Resolve app list** job logs the parsed slugs). |
| Dashboard shows no reviews / a CORS error in the console | The R2 bucket needs a CORS policy allowing GET from the dashboard's origin, OR the sync hasn't run yet for this app, OR the `?app=` slug is wrong. The console `[reviews]` logs pinpoint which. |
| Review shown without a Suggested Reply | `CODEX_AUTH_JSON_BASE_64` missing/stale, or the Codex call failed that run — the sync is unaffected. It won't get a suggestion later; write the reply manually. |
| Reply button → "token rejected" | The token is expired, lacks Actions: Read and write on `store-reviews`, or is pending org approval. |
| Reply button → 404 | `reply-review.yml` must be on `main`, and the token's repository access must include `store-reviews`. |
| No Slack notification but the dashboard has data | Wrong `SLACK_CHANNEL_ID`, bot not in the channel, or `SITE_BASE_URL` unset (data still publishes). |

## Repository Layout

```
.github/workflows/review-sync.yml    Workflow 1: cron fans out over apps.json, syncs each app, publishes to R2, notifies Slack
.github/workflows/reply-review.yml   Workflow 2: publish a dashboard-approved reply to the store
apps.json                            The app list — Infisical project slugs the scheduled sync runs for
scripts/                             Python sync + reply logic (providers + shared helpers)
state/<project_slug>/                Per-app sync state (committed by the workflows)
tests/                               Unit tests (run locally via tests/run_all.py before pushing)
```
