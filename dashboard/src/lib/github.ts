import type { Platform } from "./types"

// The reply workflow lives in the central review-sync repo.
const OWNER = import.meta.env.VITE_GITHUB_OWNER || "Lascade-Co"
const REPO = import.meta.env.VITE_GITHUB_REPO || "store-reviews"
const WORKFLOW = import.meta.env.VITE_REPLY_WORKFLOW || "reply-review.yml"
const REF = import.meta.env.VITE_REPLY_REF || "main"

const TOKEN_KEY = "gh_dispatch_token"

// Fine-grained PAT format (verified against GitHub's token structure):
// `github_pat_` + 22 alphanumerics + `_` + 59 alphanumerics = 93 chars total.
// This is a shape check (the real second segment carries a checksum), so it
// catches obvious typos/garbage before a wasted API round-trip; GitHub is the
// final authority on validity.
const FINE_GRAINED_PAT = /^github_pat_[A-Za-z0-9]{22}_[A-Za-z0-9]{59}$/

export function isValidToken(token: string): boolean {
  return FINE_GRAINED_PAT.test(token.trim())
}

export type TokenCheck =
  | { ok: true }
  | { ok: false; reason: "invalid" | "no-access" | "network" | "other"; message: string }

/**
 * Verify a token online with a side-effect-free read: GET /repos/{owner}/{repo}.
 * The token's mandatory "Metadata: read" permission is enough to read the repo
 * object, so this confirms the token is real, not expired, AND can see the repo
 * the reply workflow lives in — without triggering anything.
 *   200 → valid + has repo access
 *   401 → token invalid or expired
 *   404 → token valid but cannot see this repo (fine-grained tokens 404, not 403)
 */
export async function verifyToken(token: string): Promise<TokenCheck> {
  let res: Response
  try {
    res = await fetch(`https://api.github.com/repos/${OWNER}/${REPO}`, {
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token.trim()}`,
        "X-GitHub-Api-Version": "2022-11-28",
      },
    })
  } catch {
    return { ok: false, reason: "network", message: "Could not reach GitHub to verify the token — check your connection." }
  }
  if (res.status === 200) return { ok: true }
  if (res.status === 401) {
    return { ok: false, reason: "invalid", message: "Token is invalid or expired — create a new one and paste it here." }
  }
  if (res.status === 404) {
    return {
      ok: false,
      reason: "no-access",
      message: `Token is valid but cannot access ${OWNER}/${REPO}. Give it access to this repository (and org approval if required).`,
    }
  }
  if (res.status === 403) {
    return {
      ok: false,
      reason: "other",
      message: "GitHub returned 403 (forbidden or rate-limited) — wait a moment and try again.",
    }
  }
  return { ok: false, reason: "other", message: `GitHub returned HTTP ${res.status} while verifying the token.` }
}

export function getToken(): string | null {
  return localStorage.getItem(TOKEN_KEY)
}

export function saveToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token.trim())
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY)
}

export class AuthError extends Error {}

/**
 * Trigger the reply workflow. GitHub returns 204 on success — the workflow
 * then sends the reply to the store and removes the review from the data file.
 */
export async function dispatchReply(input: {
  project_slug: string
  platform: Platform
  review_id: string
  reply_text: string
}): Promise<void> {
  const token = getToken()
  if (!token) throw new AuthError("No access token saved")

  console.info(`[reply] dispatching ${WORKFLOW}@${REF} on ${OWNER}/${REPO}`, {
    platform: input.platform,
    review_id: input.review_id,
    reply_length: input.reply_text.length,
  })
  const res = await fetch(
    `https://api.github.com/repos/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}/dispatches`,
    {
      method: "POST",
      headers: {
        Accept: "application/vnd.github+json",
        Authorization: `Bearer ${token}`,
        "X-GitHub-Api-Version": "2022-11-28",
      },
      body: JSON.stringify({ ref: REF, inputs: input }),
    },
  )

  if (res.status === 204) {
    console.info("[reply] dispatch accepted (204) — watch the run at " +
      `https://github.com/${OWNER}/${REPO}/actions/workflows/${WORKFLOW}`)
    return
  }
  const detail = await res.text().catch(() => "")
  console.error(`[reply] dispatch FAILED: HTTP ${res.status}`, detail.slice(0, 300))
  if (res.status === 401 || res.status === 403) {
    console.error("[reply] token rejected — it may be expired, mistyped, or missing the 'Actions: Read and write' permission on this repository. It has been cleared; enter a fresh one.")
    clearToken()
    throw new AuthError("Access token was rejected — please enter a valid token")
  }
  if (res.status === 404) {
    console.error(`[reply] 404 — GitHub can't see ${WORKFLOW} on branch '${REF}' of ${OWNER}/${REPO}. Causes: reply workflow not merged to the default branch yet, OR the token's repository access doesn't include this repo (fine-grained tokens get 404, not 403, for repos they can't see).`)
  }
  if (res.status === 422) {
    console.error("[reply] 422 — the workflow rejected the inputs (name mismatch or missing input). Compare the dispatched inputs above with reply-review.yml's inputs.")
  }
  throw new Error(`Dispatch failed (HTTP ${res.status}) ${detail.slice(0, 200)}`)
}
