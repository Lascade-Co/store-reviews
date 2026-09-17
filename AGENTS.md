# Dashboard deployment

`dashboard/` is a static Vite app deployed to Cloudflare Pages by the central
`Lascade-Co/actions` static Pages runner. The caller is
`.github/workflows/dashboard-deploy.yml`; changes on `main` trigger deployment.
The caller supplies `base_path: dashboard`; `dashboard/wrangler.json` owns the target.
Use the tracked npm lockfile. Production fetches R2 directly, so verify browser
CORS for the deployed origin. Keep `SITE_BASE_URL` aligned with the Pages URL.
