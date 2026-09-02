
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
> 3. **If you changed any code, run all tests before pushing** — one command:
>    ```
>    python3 tests/run_all.py
>    ```
>    The workflow itself does not run tests, so this is the only safety check. Only push when it
>    ends with `OK`.






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
- Each review message includes an **AI-suggested reply** (generated via OpenAI, in the review's
  own language). **React to the review message with any emoji** to send the suggestion to the store
  on the next run — or ignore it and type your own reply in the thread as usual.

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
> is configured once as a GitHub Actions secret on this central repo. Onboarding only requires
> inviting that bot to your channel (Step 1). (Optional: an app can use its own bot by adding a
> non-empty `SLACK_BOT_TOKEN` to its `/reviews` folder — it then overrides the shared default.)

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
| `APPSTORE_APPLE_ID` | Numeric Apple app ID | iOS |
| `GOOGLE_PLAY_PACKAGE_NAME` | e.g. `com.lascade.myapp` | Android |
| `GOOGLE_PLAY_SERVICE_ACCOUNT_JSON` | Raw service-account JSON (**not** base64) | Android |
| `SLACK_CHANNEL_ID` | The channel ID from Step 1 | both |

<ins>For more detail on how to get each of these values, scroll down to [Getting the credentials — exact steps](#getting-the-credentials--exact-steps).</ins>

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
    # Twice daily at ~05:00 and ~15:00 IST (GitHub cron is UTC).
    # Same cron for every app — the stagger step below spreads apps out.
    - cron: "30 23,9 * * *"

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
  (`30 23,9 * * *` = 05:00 & 15:00 IST), and the "Stagger start" step delays each app by a fixed
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

# Getting the credentials — exact steps

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


[SLACK_CHANNEL_ID](https://app.slack.com)  
`(Tells the bot which Slack channel receives this app's reviews)`

1. In Slack, open the channel → click the channel name → View channel details.
2. Scroll to the bottom of the About tab and copy the Channel ID (starts with C).

---

## Day-to-day usage (for developers answering reviews)

- New reviews appear as messages (🍎 App Store / 🤖 Play Store), each with its own thread and a
  **Suggested Reply** written by AI.
- **To send the suggested reply:** react to the review message with **any emoji** (👍, ✅, anything).
  The next run publishes the suggestion as the official store response. ⚠️ Because *any* emoji
  counts as approval, don't react to a review message unless you mean "send it".
- **To answer with your own words:** reply inside its thread. The next scheduled run sends it to
  the store. A typed reply always wins over a reaction.
- **Changed your mind after a reaction sent the suggestion?** Just type your reply in the thread
  within 2 days — it replaces the suggestion on the store (no need to remove the reaction; once
  any response has been sent, reactions are ignored for that review).
- **To change your answer:** reply again in the same thread within **2 days** — the newest reply
  replaces the store response.
- Bot messages, system messages, and edits of the original review message are ignored; only
  ordinary human thread replies count. The **newest** reply is the one sent.
- Google Play replies longer than 350 characters are truncated; write short.
- Need it sent *now*? Trigger the workflow manually (Step 4.1) instead of waiting for the schedule.

## Schedule

Twice daily per app — around **05:00 and 15:00 IST** (`cron: "30 23,9 * * *"` UTC, plus each
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
| Review posted without a Suggested Reply section | `OPENAI_API_KEY` (central repo GitHub secret) missing, or the OpenAI call failed for that run — the sync itself is unaffected. A review posted without a suggestion never gets one later; reply in the thread instead. |
| Reacted to a review but nothing was sent | Reactions only work while **no** response has been sent yet, the review must still be inside its 7-day window, and the review message must contain a Suggested Reply section. Otherwise type the reply in the thread. |
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
