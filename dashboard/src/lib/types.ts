export type Platform = "appstore" | "playstore"

export interface Review {
  platform: Platform
  review_id: string
  rating: number
  title: string | null
  body: string
  reviewer: string
  territory_or_language: string
  reviewed_at: string | null
  detected_at: string | null
  suggested_reply: string | null
  replied: boolean
  reply_text: string | null
}

export interface ReviewPayload {
  project_slug: string
  generated_at: string
  reviews: Review[]
}
