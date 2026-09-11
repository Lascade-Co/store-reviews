# Store Reviews Dashboard

Web UI for the centralised review system (Lascade-Co/store-reviews). Shows each app's pending
App Store / Google Play reviews from the R2 data file and posts replies by dispatching the
reply-review workflow.

## Usage

Open the site with the app's slug as a query param (the Slack notification links do this):

    https://store-reviews-dashboard.prapanch.workers.dev/?app=airlines70

First reply prompts for a fine-grained GitHub token (repo: store-reviews only, permission
"Actions: Read and write"); it is stored in the browser's localStorage.

## Development

    npm install
    npm run dev          # uses bundled sample data (public/sample-data/demo.json): /?app=demo

## Deploy

    npm run build        # the production data URL is hardcoded in src/lib/data.ts
    # upload dist/ to the Cloudflare Worker (static assets)

Optional build-time overrides: see .env.example. Debugging: the browser console logs every
data fetch and reply dispatch with failure-specific hints ([reviews] / [reply] prefixes).
