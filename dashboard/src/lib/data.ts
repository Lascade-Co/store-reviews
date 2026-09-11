import type { ReviewPayload } from "./types"

// Where the per-app data files live (R2 public base URL incl. the random
// prefix). This is public information — it ships inside the JS bundle either
// way — so the production URL is hardcoded as the default; VITE_DATA_BASE_URL
// can still override it, and dev builds (npm run dev) use the bundled sample
// data unless overridden.
const PROD_DATA_BASE_URL = "https://store-reviews.lascadian.com/fa880a0ed915c24a2a73"
// Dev default "/r2" is a Vite dev-server proxy to the real R2 domain (see
// vite.config.ts) — local dev shows REAL data without any CORS involvement.
// Use VITE_DATA_BASE_URL=/sample-data with ?app=demo for offline sample data.
const DATA_BASE_URL: string =
  import.meta.env.VITE_DATA_BASE_URL ||
  (import.meta.env.DEV ? "/r2" : PROD_DATA_BASE_URL)

/** Parse the plain-JSON payload and validate its shape. */
function decodePayload(blob: string): ReviewPayload {
  const parsed = JSON.parse(blob)
  if (!parsed || !Array.isArray(parsed.reviews)) {
    throw new Error("Data file has an unexpected shape")
  }
  return parsed as ReviewPayload
}

/** Fetch an app's pending-reviews file. Cache-busted so updates show instantly. */
export async function fetchReviews(slug: string): Promise<ReviewPayload> {
  const url = `${DATA_BASE_URL}/${encodeURIComponent(slug)}.json?t=${Date.now()}`
  console.info(`[reviews] data base URL: ${DATA_BASE_URL}` +
    (import.meta.env.VITE_DATA_BASE_URL ? " (from VITE_DATA_BASE_URL)" : import.meta.env.DEV ? " (dev fallback: bundled sample data)" : " (built-in production URL)"))

  const pageOrigin = window.location.origin
  const dataOrigin = new URL(url, window.location.href).origin
  console.info(`[reviews] page origin:  ${pageOrigin}`)
  console.info(`[reviews] data origin:  ${dataOrigin}`)
  console.info(`[reviews] cross-origin: ${pageOrigin !== dataOrigin} ${pageOrigin !== dataOrigin ? "→ browser will require a CORS header on the response" : "→ same origin, no CORS involved"}`)
  console.info(`[reviews] request: GET ${url}`)
  const startedAt = performance.now()

  let res: Response
  try {
    res = await fetch(url)
  } catch (err) {
    // fetch() rejects only on network-level failure — for a URL that works in
    // curl/address bar, this almost always means the bucket has NO CORS policy
    // allowing this site's origin.
    console.error("[reviews] fetch was BLOCKED before reaching the server.", err)
    console.error(`[reviews] likely cause: missing CORS policy on the data domain for origin ${window.location.origin} — ask the backend team to allow GET from this origin. (Other causes: offline, DNS.)`)
    throw new Error("Could not reach the review data (likely CORS not configured — see browser console)")
  }

  console.info(`[reviews] response: HTTP ${res.status} in ${Math.round(performance.now() - startedAt)}ms (type: ${res.type})`)
  const headerLines: string[] = []
  res.headers.forEach((value, key) => headerLines.push(`    ${key}: ${value}`))
  console.info(`[reviews] response headers visible to JS:\n${headerLines.join("\n") || "    (none)"}`)
  const acao = res.headers.get("access-control-allow-origin")
  console.info(`[reviews] access-control-allow-origin: ${acao ?? "(absent)"} ${acao ? "✓ CORS grants access" : ""}`)
  if (res.status === 404) {
    console.error(`[reviews] 404 — no object at this path. Check: (a) app slug "${slug}" spelled exactly like the workflow's PROJECT_SLUG, (b) the prefix in VITE_DATA_BASE_URL matches the workflow's R2_DATA_PREFIX, (c) the sync workflow has run at least once for this app.`)
    throw new Error(`No review data found for app "${slug}"`)
  }
  if (!res.ok) {
    console.error(`[reviews] unexpected HTTP ${res.status} from data host`)
    throw new Error(`Could not load review data (HTTP ${res.status})`)
  }

  const body = await res.text()
  const contentType = res.headers.get("content-type") ?? "(unknown)"
  if (body.trimStart().startsWith("<")) {
    console.error(`[reviews] received HTML instead of JSON (content-type: ${contentType}) — the path does not exist on this server and its SPA fallback returned the app page. Check the data base URL and that the file exists.`)
    throw new Error("Data path returned a web page instead of JSON (wrong path or file missing)")
  }
  try {
    const payload = decodePayload(body)
    console.info(`[reviews] loaded ${payload.reviews.length} pending review(s), generated_at=${payload.generated_at}`)
    return payload
  } catch (err) {
    console.error("[reviews] file fetched but could not be parsed as the expected JSON shape.", err)
    console.error(`[reviews] first 200 chars of body: ${body.slice(0, 200)}`)
    throw new Error("Review data file is malformed (see browser console)")
  }
}
