import { useCallback, useEffect, useMemo, useState } from "react"
import { Send, Sparkles, PencilLine, Check, RotateCcw, X } from "lucide-react"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { Textarea } from "@/components/ui/textarea"
import { Toaster, toast } from "@/components/ui/toast"
import { fetchReviews } from "@/lib/data"
import { AuthError, dispatchReply, getToken, isValidToken, saveToken, verifyToken } from "@/lib/github"
import type { Review, ReviewPayload } from "@/lib/types"

// Store reply length caps. Google Play rejects replies over ~350 chars (hard
// API limit, backend truncates too). Apple has no documented API length limit;
// the ~5970 figure is the App Store Connect UI cap — used as a generous guard.
const REPLY_LIMIT: Record<Review["platform"], number> = {
  playstore: 350,
  appstore: 5970,
}
const REPO_NAME = `${import.meta.env.VITE_GITHUB_OWNER || "Lascade-Co"}/${
  import.meta.env.VITE_GITHUB_REPO || "store-reviews"
}`

function reviewKey(review: Review): string {
  return `${review.platform}-${review.review_id}`
}

function appSlugFromUrl(): string {
  return new URLSearchParams(window.location.search).get("app")?.trim() ?? ""
}

function relativeTime(iso: string | null): string {
  if (!iso) return "unknown"
  const then = new Date(iso).getTime()
  if (isNaN(then)) return "unknown"
  const minutes = Math.max(0, Math.round((Date.now() - then) / 60000))
  if (minutes < 1) return "just now"
  if (minutes < 60) return `${minutes} min ago`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} hour${hours === 1 ? "" : "s"} ago`
  const days = Math.round(hours / 24)
  return `${days} day${days === 1 ? "" : "s"} ago`
}

function formatDate(iso: string | null): string {
  if (!iso) return "—"
  const date = new Date(iso)
  return isNaN(date.getTime())
    ? "—"
    : date.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
        hour: "numeric",
        minute: "2-digit",
      })
}

const STAR_PATH =
  "M12 2l2.9 6.3 6.9.7-5.1 4.6 1.4 6.8L12 17.8 5.9 20.4l1.4-6.8L2.2 9l6.9-.7L12 2z"

function Star({ filled }: { filled: boolean }) {
  return (
    <svg viewBox="0 0 24 24" className="size-4" aria-hidden>
      <path
        d={STAR_PATH}
        fill={filled ? "#FBBF24" : "#9B9B9B"}
        stroke={filled ? "#FBBF24" : "#E2E8F0"}
        strokeWidth={filled ? 0 : 2}
        strokeLinejoin="round"
      />
    </svg>
  )
}

function Stars({ rating }: { rating: number }) {
  const filled = Math.max(0, Math.min(5, rating))
  return (
    <span className="flex items-center gap-0.5">
      {Array.from({ length: 5 }, (_, index) => (
        <Star key={index} filled={index < filled} />
      ))}
    </span>
  )
}

function AppleLogo() {
  return (
    <svg viewBox="0 0 384 512" className="size-4 fill-black" aria-hidden>
      <path d="M318.7 268.7c-.2-36.7 16.4-64.4 50-84.8-18.8-26.9-47.2-41.7-84.7-44.6-35.5-2.8-74.3 20.7-88.5 20.7-15 0-49.4-19.7-76.4-19.7C63.3 141.2 4 184.8 4 273.5q0 39.3 14.4 81.2c12.8 36.7 59 126.7 107.2 125.2 25.2-.6 43-17.9 75.8-17.9 31.8 0 48.3 17.9 76.4 17.9 48.6-.7 90.4-82.5 102.6-119.3-65.2-30.7-61.7-90-61.7-91.9zm-56.6-164.2c27.3-32.4 24.8-61.9 24-72.5-24.1 1.4-52 16.4-67.9 34.9-17.5 19.8-27.8 44.3-25.6 71.9 26.1 2 49.9-11.4 69.5-34.3z" />
    </svg>
  )
}

function PlayLogo() {
  return (
    <svg viewBox="0 0 512 512" className="size-4" aria-hidden>
      <path fill="#00D7FE" d="M47 0C34 6.8 25.3 19.2 25.3 35.3v441.3c0 16.1 8.7 28.5 21.7 35.3l256.6-256L47 0z" />
      <path fill="#00F076" d="M325.3 234.3 104.6 13l280.8 161.2-60.1 60.1z" />
      <path fill="#FFD200" d="m472.2 225.6-58.9-34.1-65.7 64.5 65.7 64.5 60.1-34.1c18-14.3 18-46.5-1.2-60.8z" />
      <path fill="#F53E5B" d="m104.6 499 280.8-161.2-60.1-60.1L104.6 499z" />
    </svg>
  )
}

function PlatformLabel({ platform }: { platform: Review["platform"] }) {
  return (
    <span className="inline-flex items-center gap-1 font-semibold text-slate-700">
      {platform === "appstore" ? (
        <>
          <AppleLogo /> App Store
        </>
      ) : (
        <>
          <PlayLogo /> Google Play
        </>
      )}
    </span>
  )
}

export default function App() {
  const slug = useMemo(appSlugFromUrl, [])
  const [payload, setPayload] = useState<ReviewPayload | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)

  // Per-review editable reply drafts, seeded from the AI suggestion.
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [sendingKey, setSendingKey] = useState<string | null>(null)
  const [dispatched, setDispatched] = useState<Set<string>>(new Set())

  const [tokenDialogOpen, setTokenDialogOpen] = useState(false)
  const [tokenDraft, setTokenDraft] = useState("")
  const [tokenError, setTokenError] = useState<string | null>(null)
  const [verifyingToken, setVerifyingToken] = useState(false)
  const [pendingKey, setPendingKey] = useState<string | null>(null)
  // The "Access token needed" notification (null = hidden). message varies by
  // whether it's the first request or a rejected token.
  const [tokenNotice, setTokenNotice] = useState<string | null>(null)

  const load = useCallback(async () => {
    if (!slug) {
      console.warn("[app] no ?app=<slug> query param — showing the landing screen")
      return
    }
    console.info(`[app] loading reviews for app "${slug}"`)
    setLoading(true)
    setError(null)
    try {
      const data = await fetchReviews(slug)
      console.info(`[app] render: ${data.reviews.length} pending review(s) for ${data.project_slug}`)
      setPayload(data)
      setDrafts((prev) => {
        const next = { ...prev }
        for (const review of data.reviews) {
          const key = reviewKey(review)
          if (!(key in next)) next[key] = review.suggested_reply ?? ""
        }
        return next
      })
    } catch (err) {
      console.error("[app] load failed:", err)
      setError(err instanceof Error ? err.message : String(err))
      setPayload(null)
    } finally {
      setLoading(false)
    }
  }, [slug])

  useEffect(() => {
    void load()
  }, [load])

  async function send(review: Review) {
    const key = reviewKey(review)
    const text = (drafts[key] ?? "").trim()
    console.info(`[app] Reply clicked: ${key} (${text.length} chars, ${text === (review.suggested_reply ?? "").trim() ? "AI suggestion" : "custom text"})`)
    if (!text) return
    if (!getToken()) {
      // First request: show the notification (not the dialog); the dialog
      // opens only when the developer clicks "Enter Token".
      console.info("[app] no saved token — showing token notice")
      setPendingKey(key)
      setTokenNotice("Set up an access token once to publish replies from this dashboard.")
      return
    }
    setSendingKey(key)
    try {
      await dispatchReply({
        project_slug: slug,
        platform: review.platform,
        review_id: review.review_id,
        reply_text: text,
      })
      setDispatched((prev) => new Set(prev).add(key))
      toast.add({
        title: "Reply sent",
        description: "Publishing to the store (~1 minute).",
      })
    } catch (err) {
      if (err instanceof AuthError) {
        setPendingKey(key)
        setTokenNotice("Access token was rejected — please enter a valid token to auto-publish replies.")
      } else {
        toast.add({
          title: "Reply failed",
          description: err instanceof Error ? err.message : String(err),
        })
      }
    } finally {
      setSendingKey(null)
    }
  }

  if (!slug) {
    return (
      <main className="mx-auto flex min-h-svh max-w-xl flex-col items-center justify-center gap-3 p-8 text-center">
        <h1 className="text-2xl font-bold">Review Management</h1>
        <p className="text-muted-foreground">
          Open this page from the link in your app's Slack channel — it carries
          your app's id, e.g. <code>?app=airlines70</code>.
        </p>
      </main>
    )
  }

  const reviews = payload?.reviews ?? []

  return (
    <main className="min-h-svh bg-slate-50 antialiased">
      <Toaster />
      <div className="mx-auto w-full max-w-7xl px-4 py-6 md:px-8 space-y-6">
        {/* Header */}
        <header className="flex flex-wrap items-center justify-between gap-4">
          <div>
            <h1 className="text-2xl font-bold tracking-tight text-slate-900">Review Management</h1>
            <p className="mt-1 text-sm text-slate-500">
              Manage and respond to your Play Store and App Store reviews in one place.
            </p>
          </div>
          <span className="text-xs font-medium text-slate-400">
            Last updated: {relativeTime(payload?.generated_at ?? null)}
          </span>
        </header>

        {/* App card */}
        <section className="rounded-xl border border-slate-200 bg-white p-4 shadow-sm">
          <p className="text-xs font-medium text-slate-500">App</p>
          <div className="mt-1 flex flex-wrap items-center gap-3">
            <h2 className="text-xl font-bold capitalize tracking-tight text-slate-900">{slug.replaceAll("_", " ")}</h2>
            <Badge className="bg-emerald-100 text-emerald-700 hover:bg-emerald-100">
              Active
            </Badge>
          </div>
        </section>

        {/* Reviews */}
        <section>
          <div className="mb-4 flex items-center gap-2">
            <h3 className="text-base font-semibold text-slate-900">New Reviews</h3>
            {reviews.length > 0 && (
              <span className="inline-flex h-5 min-w-5 items-center justify-center rounded-full bg-red-500 px-1.5 text-xs font-bold text-white">
                {reviews.length}
              </span>
            )}
          </div>

          {error && (
            <p className="rounded-md border border-destructive/50 bg-destructive/10 p-4 text-sm">
              {error}
            </p>
          )}
          {loading && !payload && (
            <p className="p-6 text-center text-muted-foreground">Loading…</p>
          )}
          {payload && reviews.length === 0 && (
            <p className="rounded-md border border-dashed p-8 text-center text-muted-foreground">
              🎉 No reviews awaiting a reply.
            </p>
          )}

          <div className="space-y-4">
            {reviews.map((review) => {
              const key = reviewKey(review)
              const draft = drafts[key] ?? ""
              const isCustom =
                draft.trim() !== (review.suggested_reply ?? "").trim() ||
                !review.suggested_reply
              const isDispatched = dispatched.has(key)
              const limit = REPLY_LIMIT[review.platform]
              const atLimit = draft.length >= limit
              const canRestore = !!review.suggested_reply && isCustom && !isDispatched
              return (
                <article
                  key={key}
                  className="group rounded-2xl border border-slate-200 bg-white p-5 shadow-sm transition-all hover:border-slate-300 hover:shadow-md"
                >
                  <div className="grid grid-cols-1 lg:grid-cols-12 gap-6 items-start">
                  {/* Review content */}
                  <div className="lg:col-span-6 space-y-3">
                    <div className="flex items-start justify-between gap-4">
                      <div className="space-y-1">
                        <p className="text-sm font-bold text-slate-900 tracking-tight">{review.reviewer}</p>
                        <div className="flex items-center gap-2 text-xs text-slate-500 font-medium">
                          <PlatformLabel platform={review.platform} />
                          <span>•</span>
                          <span className="rounded bg-slate-100 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wider text-slate-600">
                            {review.territory_or_language}
                          </span>
                        </div>
                      </div>
                      <Stars rating={review.rating} />
                    </div>

                    <div className="space-y-1.5 pt-1">
                    {review.title && (
                      <h4 className="text-sm font-bold text-slate-900">{review.title}</h4>
                    )}
                    <p className="whitespace-pre-wrap text-xs leading-relaxed text-slate-600">
                      {review.body}
                    </p>
                    </div>
                    <div className="flex items-center gap-2 text-xs text-slate-400 pt-1">
                      <span className="inline-flex items-center gap-1">
                        🗓 Posted: {formatDate(review.reviewed_at)}
                      </span>
                    </div>
                  </div>

                  {/* AI reply panel */}
                  <div className="lg:col-span-6 flex flex-col rounded-xl border border-violet-100 bg-gradient-to-b from-violet-50/40 to-slate-50/60 p-4 space-y-3">
                    <div className="flex items-center justify-between">
                      <p className="flex items-center gap-1.5 text-xs font-semibold text-violet-700">
                        {isCustom ? (
                          <>
                            <PencilLine className="size-3.5" /> Custom Reply
                          </>
                        ) : (
                          <>
                            <Sparkles className="size-3.5" /> AI Suggested Reply
                          </>
                        )}
                      </p>
                      {canRestore && (
                        <button
                          type="button"
                          onClick={() =>
                            setDrafts((prev) => ({ ...prev, [key]: review.suggested_reply ?? "" }))
                          }
                          className="flex items-center gap-1 text-[11px] font-medium text-slate-500 hover:text-violet-600 transition-colors"
                          title="Restore the original AI suggestion"
                        >
                          <RotateCcw className="size-3" /> Restore
                        </button>
                      )}
                    </div>

                    <Textarea
                      value={draft}
                      maxLength={limit}
                      onChange={(event) =>
                        setDrafts((prev) => ({ ...prev, [key]: event.target.value.slice(0, limit) }))
                      }
                      disabled={isDispatched}
                      rows={4}
                      placeholder="Write a reply…"
                      className={`min-h-[100px] flex-1 resize-y rounded-lg bg-white text-xs leading-relaxed text-slate-800 shadow-sm focus:ring-1 ${
                        atLimit
                          ? "border-red-500 focus:border-red-500 focus:ring-red-500"
                          : "border-slate-200 focus:border-violet-500 focus:ring-violet-500"
                      }`}
                    />

                    <div className="flex items-center justify-between gap-2 pt-1">
                      <span
                        key={atLimit ? "at" : "under"}
                        className={`text-[11px] font-semibold tabular-nums ${
                          atLimit ? "animate-limit-shake text-red-500" : "text-slate-400"
                        }`}
                      >
                        {draft.length}/{limit} chars
                      </span>
                      {isDispatched ? (
                        <Badge className="bg-emerald-100 text-emerald-700 hover:bg-emerald-100">
                          <Check className="mr-1 size-3" /> Replied
                        </Badge>
                      ) : (
                        <Button
                          size="sm"
                          className="inline-flex items-center gap-1.5 rounded-lg bg-indigo-600 px-4 text-xs font-semibold text-white shadow-sm hover:bg-indigo-700 transition-all"
                          onClick={() => void send(review)}
                          disabled={sendingKey === key || !draft.trim()}
                        >
                          {sendingKey === key ? "Sending…" : "Reply"}
                          <Send className="size-3" />
                        </Button>
                      )}
                    </div>
                  </div>
                  </div>
                </article>
              )
            })}
          </div>
        </section>
      </div>

      {/* Access-token notification (action card, bottom-right) */}
      {tokenNotice && (
        <div className="fixed right-4 bottom-4 z-50 w-[360px] max-w-[calc(100vw-2rem)] rounded-xl border border-slate-200 bg-white p-4 shadow-xl">
          <button
            type="button"
            onClick={() => setTokenNotice(null)}
            className="absolute right-3 top-3 text-slate-400 hover:text-slate-600"
            aria-label="Dismiss"
          >
            <X className="size-4" />
          </button>
          <div className="flex gap-3">
            <span className="mt-0.5 flex size-5 shrink-0 items-center justify-center rounded-full bg-red-500 text-xs font-bold text-white">
              !
            </span>
            <div className="pr-4">
              <p className="text-sm font-semibold text-slate-900">Access token needed</p>
              <p className="mt-1 text-sm text-slate-600">{tokenNotice}</p>
              <div className="mt-3 flex items-center gap-4">
                <Button
                  size="sm"
                  className="bg-slate-900 text-white hover:bg-slate-800"
                  onClick={() => {
                    setTokenNotice(null)
                    setTokenDialogOpen(true)
                  }}
                >
                  Enter Token
                </Button>
                <button
                  type="button"
                  onClick={() => setTokenNotice(null)}
                  className="text-sm font-medium text-slate-500 hover:text-slate-700"
                >
                  Dismiss
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* First-use token dialog */}
      <Dialog open={tokenDialogOpen} onOpenChange={setTokenDialogOpen}>
        <DialogContent className="sm:max-w-lg border-slate-200 bg-white text-slate-900 shadow-xl">
          <DialogHeader>
            <DialogTitle>GitHub access token needed</DialogTitle>
            <DialogDescription>
              Create a fine-grained personal access token with access to ONLY
              the {REPO_NAME} repository and the single permission "Actions:
              Read and write", then paste it here. It is stored only in this
              browser.
            </DialogDescription>
          </DialogHeader>
          <Input
            type="password"
            className={`bg-white ${tokenError ? "border-red-500 focus:border-red-500 focus:ring-red-500" : ""}`}
            value={tokenDraft}
            onChange={(event) => {
              setTokenDraft(event.target.value)
              if (tokenError) setTokenError(null)
            }}
            placeholder="github_pat_…"
          />
          {tokenError && <p className="text-xs font-medium text-red-500">{tokenError}</p>}
          <DialogFooter>
            <Button
              variant="outline"
              onClick={() => {
                setTokenError(null)
                setTokenDialogOpen(false)
              }}
            >
              Cancel
            </Button>
            <Button
              className="bg-indigo-600 text-white hover:bg-indigo-700"
              disabled={!tokenDraft.trim() || verifyingToken}
              onClick={() => {
                void (async () => {
                  // 1) local format check (instant, no request)
                  if (!isValidToken(tokenDraft)) {
                    setTokenError(
                      "That doesn't look like a fine-grained token. It should start with \"github_pat_\" and be 93 characters long.",
                    )
                    return
                  }
                  // 2) online check — real token, not expired, can see the repo
                  setTokenError(null)
                  setVerifyingToken(true)
                  const check = await verifyToken(tokenDraft)
                  setVerifyingToken(false)
                  if (!check.ok) {
                    setTokenError(check.message)
                    return
                  }
                  // valid — only now persist it
                  saveToken(tokenDraft)
                  setTokenDraft("")
                  setTokenDialogOpen(false)
                  const retry = pendingKey
                  setPendingKey(null)
                  toast.add({
                    title: "Token verified & saved",
                    description: retry
                      ? "Click Reply again to send."
                      : "You can now send replies.",
                  })
                })()
              }}
            >
              {verifyingToken ? "Verifying…" : "Save token"}
            </Button>
          </DialogFooter>
        </DialogContent>
      </Dialog>
    </main>
  )
}
