# Store Reviews Dashboard

Web UI for the centralised review system (Lascade-Co/store-reviews). Shows each app's pending
App Store / Google Play reviews from the R2 data file and posts replies by dispatching the
reply-review workflow.

## Usage

Open the site with the app's slug as a query param (the Slack notification links do this):

    https://store-reviews-dashboard.pages.dev/?app=airlines70

First reply prompts for a fine-grained GitHub token (repo: store-reviews only, permission
"Actions: Read and write"); it is stored in the browser's localStorage.

## Development

    npm ci
    npm run dev          # proxies real R2 data through /r2

For offline sample data, run `VITE_DATA_BASE_URL=/sample-data npm run dev` and
open `/?app=demo`.

## Deploy

Push dashboard changes to `main`, or run **Deploy Dashboard** manually on `main`.
The source workflow calls the reusable static Pages trigger in `Lascade-Co/actions`
with `base_path: dashboard`. The common runner reads the Cloudflare account and
Pages project from this directory's `wrangler.json` at that exact source commit.
That repository builds the exact source commit with `npm ci`, lint, and build,
then uploads `dist/` to the `store-reviews-dashboard` Cloudflare Pages project.
Check the central **Static Pages Deploy** run for deployment completion; a green
source trigger only means the deployment request was accepted.

The source uses its existing `CI_APP_CLIENT_ID` and `CI_APP_PRIVATE_KEY` secrets
to dispatch to the central repo. Cloudflare deployment credentials stay in the
central repo. Pages uses Direct Upload through Actions, not built-in Git integration.

Production defaults are in `src/lib/data.ts` and `src/lib/github.ts`; no build
secrets are needed. If overrides become necessary, set the trigger's optional
`project_slug` and `infisical_path` inputs to import build values from Infisical's
`prod` environment. Keep only browser-safe build values in that folder.
Optional public build-time overrides are `VITE_DATA_BASE_URL`,
`VITE_GITHUB_OWNER`, `VITE_GITHUB_REPO`, `VITE_REPLY_WORKFLOW`, and `VITE_REPLY_REF`.
The R2 bucket must permit GET from `https://store-reviews-dashboard.pages.dev`.
The repository secret `SITE_BASE_URL` controls links in future Slack notifications.
Users must enter their GitHub token again when moving to the new Pages origin.

Debugging: the browser console logs every
data fetch and reply dispatch with failure-specific hints ([reviews] / [reply] prefixes).
