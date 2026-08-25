
# Store Reviews — Centralised Review Sync





> [!CAUTION]
> ## Editing this repo? Always pull before pushing — NEVER force-push
> The workflow **commits sync state to `main` on every run** (`state/<project_slug>/…`), so `main`
> moves even when no human is working.
>
> 1. **Pull right before you push:** `git pull --rebase origin main`. A "non-fast-forward"
>    rejection just means the workflow committed state in between — pull and push again; nothing
>    is broken.
> 2. **Never `git push --force` to `main`.** It rewrites history and **deletes the state commits
>    made since your last pull** — the permanent dedup ids (`posted_ids`) and Slack thread
>    mappings. The next run would then re-post already-posted reviews to Slack and lose track of
>    pending replies. A rejected push is always answered with *pull*, never *force*.






A centralised bot that syncs customer reviews from the **Apple App Store** and **Google Play** to a
per-app **Slack channel**, and sends developer replies typed in the Slack thread **back to the
store**. It runs entirely on GitHub Actions — no server, no database. State is committed to this
repo under `state/<project_slug>/`.

**How a review flows:**

```
App Store / Google Play
        │  (polled twice daily)
        ▼
  Slack channel  ──► each review = one message + its own thread
        │
        ▼
  Developer types a reply in the thread
        │  (picked up on the next run)
        ▼
  Published as the official developer response on the store
```

- Every review is posted **once** (permanent dedup — even edited/re-appearing reviews are never
  re-posted).
- The **newest**  reply in a thread wins: reply again within **2 days** to replace the
  response on the store.
- An un-replied review's thread is watched for **7 days**; after that a reply in Slack is no longer
  forwarded.
- Google Play replies are truncated to **350 characters** (store limit). Slack formatting
  (links, `&`, mentions) is automatically converted to plain text before sending.

This central repo (`Lascade-Co/store-reviews`) serves **any number of apps**. Connecting a new app
requires no code change here — only the three steps below.

---

## Connecting a new app (step by step)

### Prerequisites

| What |-> Where to get it |
|---|---|
| App Store Connect API key (`.p8`) with **Customer Reviews** read + response permission |-> App Store Connect → Users and Access → Integrations → App Store Connect API |
| Numeric Apple **App ID** (not the bundle ID) |-> App Store Connect → App → App Information |
| Google Play **service account JSON** with Play Console access to *view and reply to reviews* |-> Google Cloud Console + Play Console → Users and permissions |
| The app's **Infisical project slug** |-> secrets.lascade.com → the app's project → Settings → Project slug |

> You do **not** need the Slack bot token: one shared internal bot serves all apps, and its token
> (`SLACK_BOT_TOKEN`) is configured once as a GitHub Actions secret on this central repo.
> Onboarding only requires inviting that bot to your channel (Step 1).

An app can be iOS-only or Android-only — just skip the other platform's keys everywhere below;
that platform's job will log "not configured" and exit cleanly.

### Step 1 — Slack channel

1. Create the app's review channel in slack (e.g. `#myapp-reviews`).
2. Invite the bot: `/invite @ReviewTravel` (the shared internal review bot).
3. Copy the **channel ID** (channel name → View channel details → bottom of the About tab,
   starts with `C`).

### Step 2 — Infisical: create the `/reviews` folder

In the app's Infisical project on `secrets.lascade.com`, create a folder named **`reviews`** in the
**Production** environment (the workflow reads Production by default) and add:

| Secret | Value | Needed for |
|---|---|---|
| `APPSTORE_API_KEY_ID` | App Store Connect key ID | iOS |
| `APPSTORE_API_PRIVATE_KEY` | Full `.p8` file contents (multiline is fine) | iOS |
| `APPSTORE_ISSUER_ID` | Issuer UUID from the Integrations page | iOS |
| `APPSTORE_APP_ID` | Numeric Apple app ID | iOS |
| `GOOGLE_PLAY_PACKAGE_NAME` | e.g. `com.lascade.myapp` | Android |
| `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` | Raw service-account JSON (**not** base64) | Android |
| `SLACK_CHANNEL_ID` | The channel ID from Step 1 | both |

> The workflow always reads the **Production** environment — put the keys there.

### Step 3 — Add the trigger workflow to the app repo

Copy [`triggers/review-sync-trigger.yml`](triggers/review-sync-trigger.yml) from this repo into the
app repository at `.github/workflows/review-sync-trigger.yml` (on the **default branch** — the
schedule only fires there), and set the `project_slug`:

```yaml
name: Trigger Review Sync

on:
  workflow_dispatch:
  schedule:
    # Twice daily at ~10:00 and ~17:00 IST (GitHub cron is UTC).
    # Same cron for every app — the stagger step below spreads apps out.
    - cron: "30 4,11 * * *"

permissions:
  contents: read

jobs:
  dispatch:
    runs-on: ubuntu-latest
    steps:
      # Hashes the repo name into a fixed 0-300s delay so apps sharing the
      # cron never dispatch at the same moment. Logs the delay and a per-minute
      # countdown. Skipped on manual runs.
      - name: Stagger start (repo-specific delay; skipped on manual runs)
        if: github.event_name == 'schedule'
        run: |
          OFFSET=$(( $(cksum <<< "$GITHUB_REPOSITORY" | cut -d' ' -f1) % 300 ))
          DISPATCH_AT=$(date -u -d "+${OFFSET} seconds" '+%H:%M:%S')
          echo "Repository:       ${GITHUB_REPOSITORY}"
          echo "Stagger delay:    ${OFFSET}s (deterministic, derived from the repo name)"
          echo "Current time:     $(date -u '+%Y-%m-%d %H:%M:%S') UTC"
          echo "Dispatching at:   ${DISPATCH_AT} UTC"
          REMAINING=${OFFSET}
          while [ "${REMAINING}" -gt 0 ]; do
            STEP=$(( REMAINING < 60 ? REMAINING : 60 ))
            echo "$(date -u '+%H:%M:%S') UTC | waiting, ${REMAINING}s left (dispatch at ${DISPATCH_AT} UTC)"
            sleep "${STEP}"
            REMAINING=$(( REMAINING - STEP ))
          done
          echo "$(date -u '+%H:%M:%S') UTC | stagger complete, dispatching now"

      - name: Dispatch to central review-bot
        uses: peter-evans/repository-dispatch@v4
        with:
          token: ${{ secrets.CENTRAL_DISPATCH_TOKEN }}
          repository: Lascade-Co/store-reviews
          event-type: review-sync
          client-payload: >-
            { "project_slug": "<your-infisical-project-slug>" }
```

Notes:

- **Don't edit the cron — the schedule staggers itself.** Every app uses the same cron
  (`30 4,11 * * *` = 10:00 & 17:00 IST), and the "Stagger start" step delays each app by a fixed
  0–5-minute offset before dispatching. (Manual runs skip the delay and dispatch immediately.)

  **Why the stagger exists:** all apps share one internal Slack bot, and Slack rate-limits each
  bot per API method (about 50 thread-poll requests per minute). If every app fired at the exact
  same cron second, all their runs would hit that shared budget simultaneously. The stagger
  spreads them out so each run has the whole budget to itself.

  **How it works:** the step hashes the repository's own name (`cksum`, a CRC-32 checksum) into a
  number and takes it modulo 300, giving every repo a *deterministic* 0–299-second delay — always
  the same for the same repo, different across repos. That means apps spread themselves out with
  zero coordination between teams.

- `project_slug` is the app's **Infisical project slug** — it selects the app's `/reviews` secrets
  AND names its state folder here (`state/<project_slug>/`). It must match exactly.

- The payload carries **no secrets** — only the slug.

### Step 4 — First run and verification

1. Run it once manually: app repo → **Actions → Trigger Review Sync → Run workflow**.
2. Watch the run in `store-reviews` → Actions. Expect:
   - **Fetch review secrets from Infisical** turns the `/reviews` keys into env vars.
   - The **initial sync** posts the newest **5 reviews per platform** to the Slack channel.
   - **Commit Updated State** creates and pushes `state/<project_slug>/` automatically.
3. Reply to one review in its Slack thread, run again, and confirm the reply appears on the store
   (App Store responses can take a while to show publicly).

Done. From now on the schedule handles everything; no manual state setup is ever needed.

---

## Day-to-day usage (for developers answering reviews)

- New reviews appear as messages (🍎 App Store / 🤖 Play Store), each with its own thread.
- **To answer a review:** reply inside its thread. The next scheduled run sends it to the store.
- **To change your answer:** reply again in the same thread within **2 days** — the newest reply
  replaces the store response.
- Bot messages, system messages, and edits of the original review message are ignored; only
  ordinary human thread replies count. The **newest** reply is the one sent.
- Google Play replies longer than 350 characters are truncated; write short.
- Need it sent *now*? Trigger the workflow manually (Step 4.1) instead of waiting for the schedule.

## Schedule

Twice daily per app — around **10:00 and 17:00 IST** (`cron: "30 4,11 * * *"` UTC, plus each
app's automatic 0–5-minute repo-specific stagger) — and manual runs anytime (manual runs skip the
stagger). GitHub may delay cron by a few minutes. A review or reply posted between runs waits for
the next run.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `Error: Missing universal auth credentials` in the Infisical step | The `INFISICAL_CLIENT_ID/SECRET/DOMAIN` GitHub secrets are missing/empty on `store-reviews` (org secrets don't reach private repos on the Free plan). Ask the backend team to (re-)provision them via the Infisical→GitHub sync or repo secrets. |
| Infisical step fails with project not found | `project_slug` in the trigger payload doesn't match the Infisical **project slug** (check the project's Settings page — it's not the display name). |
| A provider logs "not configured for this app; skipping" | That platform's keys are absent from `/reviews`. Intentional for single-platform apps; otherwise add the missing keys. |
| Slack `not_in_channel` / `channel_not_found` | The bot isn't a member of the channel (Step 1.2) or `SLACK_CHANNEL_ID` is wrong. |
| Google Play returns 0 reviews | Normal: the Play API only returns reviews **created/modified in the last 7 days** that have text, production track only. |
| Trigger run fails with 404/401 on dispatch | `CENTRAL_DISPATCH_TOKEN` missing in the app repo — ask the backend team. |
| Reply typed but nothing sent | Was it >7 days after the review was posted (or >2 days after the previous reply)? The thread is no longer polled — this is the designed window. |

## Repo layout (central repo)

```
.github/workflows/review-sync.yml   central workflow (repository_dispatch + manual)
triggers/review-sync-trigger.yml    trigger template to copy into app repos
scripts/                            Python sync logic (providers + shared helpers)
state/<project_slug>/               per-app sync state (committed by the workflow)
state/_example/                     dummy state files showing the schema (see state/README.md)
tests/                              unit tests (run automatically before every sync)
```
