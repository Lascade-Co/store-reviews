# Store Review API — Response Reference (Play Store + App Store)

> Purpose: confirm what each store's review API returns, **in particular whether it
> gives back the developer's reply text**, and capture the full JSON shapes so we can
> decide what we still need to track in our own state.
>
> Compiled 2026-11-20 from the official docs (links at the bottom). Where a field's
> type was not machine-readable in Apple's stub pages, it is marked **(from example / known schema)**.

---

## TL;DR

| Question | Google Play | Apple App Store |
|---|---|---|
| Does the API return the **developer reply** text? | **Yes** | **Yes** |
| Is the reply returned **inline** with the review? | **Yes** — inside `comments[].developerComment.text` of the *same* `reviews.list` response | **No** — the review only carries a `relationships.response` **link**; you must `?include=response` or call `/v1/customerReviews/{id}/response` |
| Can you tell a review **has no reply**? | Yes — no `developerComment` entry in `comments[]` | Yes — the `response` relationship resolves to empty / 404, or is absent from `included[]` |
| Does the API tell you **who wrote** the reply (human vs our bot)? | **No** | **No** |
| Reply last-modified timestamp available? | Yes — `developerComment.lastModified` | Yes — `response.attributes.lastModifiedDate` |

**Bottom line:** both stores are a reliable source of truth for *"is this review replied to, and what does the reply say?"* Neither store tells us *who* wrote the reply — so authorship (human vs AI) is the one thing we must still track ourselves.

---

## 1. Google Play — `androidpublisher.reviews`

Endpoints:
- `GET reviews.list(packageName, maxResults, startIndex, token, translationLanguage)` — list reviews
- `GET reviews.get(packageName, reviewId, translationLanguage)` — one review
- `POST reviews.reply(packageName, reviewId, {replyText})` — reply / update reply

The developer reply is a first-class part of the review: each review has a `comments[]`
array, and each comment is a **union** of either `userComment` (the customer) or
`developerComment` (our reply).

### 1a. `reviews.list` — full response shape

```json
{
  "pageInfo": {
    "totalResults": 42,
    "startIndex": 42,
    "resultPerPage": 42
  },
  "reviews": [
    {
      "reviewId": "A String",
      "authorName": "A String",
      "comments": [
        {
          "userComment": {
            "text": "A String",
            "lastModified": { "seconds": "A String", "nanos": 42 },
            "starRating": 42,
            "reviewerLanguage": "A String",
            "device": "A String",
            "androidOsVersion": 42,
            "appVersionCode": 42,
            "appVersionName": "A String",
            "thumbsUpCount": 42,
            "thumbsDownCount": 42,
            "deviceMetadata": {
              "productName": "A String",
              "manufacturer": "A String",
              "deviceClass": "A String",
              "screenWidthPx": 42,
              "screenHeightPx": 42,
              "nativePlatform": "A String",
              "screenDensityDpi": 42,
              "glEsVersion": 42,
              "cpuModel": "A String",
              "cpuMake": "A String",
              "ramMb": 42
            },
            "originalText": "A String"
          },
          "developerComment": {
            "text": "A String",
            "lastModified": { "seconds": "A String", "nanos": 42 }
          }
        }
      ]
    }
  ],
  "tokenPagination": {
    "nextPageToken": "A String",
    "previousPageToken": "A String"
  }
}
```

> In real data each `comments[]` entry contains **exactly one** of `userComment` *or*
> `developerComment` (it is a union). A review with no reply simply has no entry whose
> key is `developerComment`. A replied review has two entries: the user's and ours.

### 1b. Key objects

**Review**
| Field | Type | Notes |
|---|---|---|
| `reviewId` | string | Unique id for the review |
| `authorName` | string | Display name of the reviewer |
| `comments[]` | Comment[] | Conversation entries (user + developer) |

**Comment** — union, one of:
| Field | Type | Notes |
|---|---|---|
| `userComment` | UserComment | The customer's review |
| `developerComment` | DeveloperComment | **Our reply** |

**UserComment**
| Field | Type | Notes |
|---|---|---|
| `text` | string | Review body. If the review had a title, title+body are joined with a **tab** char |
| `lastModified` | Timestamp | Last update time |
| `starRating` | integer | 1–5 |
| `reviewerLanguage` | string | From device settings; **may not match the written language**; may be absent |
| `device`, `androidOsVersion`, `appVersionCode`, `appVersionName` | — | Device/app context; may be absent |
| `thumbsUpCount`, `thumbsDownCount` | integer | Review helpfulness votes |
| `deviceMetadata` | DeviceMetadata | Device characteristics |
| `originalText` | string | Untranslated text when the review was auto-translated; blank otherwise |

**DeveloperComment** (our reply)
| Field | Type | Notes |
|---|---|---|
| `text` | string | **The reply body** |
| `lastModified` | Timestamp | Last time the reply was updated |

**Timestamp**
| Field | Type | Notes |
|---|---|---|
| `seconds` | string (int64) | Seconds since Unix epoch |
| `nanos` | integer | 0–999,999,999 |

### 1c. `reviews.reply` (how we post, for completeness)

Request:
```json
{ "replyText": "A String" }
```
> Replies longer than ~350 chars are rejected; HTML tags are stripped.

Response:
```json
{
  "result": {
    "replyText": "A String",
    "lastEdited": { "seconds": "A String", "nanos": 42 }
  }
}
```

---

## 2. Apple App Store — `customerReviews` + `customerReviewResponses`

Endpoints:
- `GET /v1/apps/{id}/customerReviews` or `GET /v1/appStoreVersions/{id}/customerReviews` — list reviews
- `GET /v1/customerReviews/{id}/response` — get the developer response for a review
- `GET /v1/customerReviewResponses/{id}` — read a response by its own id
- `POST /v1/customerReviewResponses` — create/update a response

Apple is a **JSON:API** design. Unlike Play, the review resource does **not** embed the
reply. The review carries a `relationships.response` object with **links only**. To get
the reply text you either:
1. add `?include=response` to the list/get call (Apple returns the response resource in
   a top-level `included[]` array), **or**
2. call `GET /v1/customerReviews/{id}/response` per review.

### 2a. `GET …/customerReviews` — full response shape (verbatim from Apple's example)

```json
{
  "data": [
    {
      "type": "customerReviews",
      "id": "00000028-b08c-0014-9674-c54800000000",
      "attributes": {
        "rating": 5,
        "title": "Pretty Stellar",
        "body": "I love how creative I can be when I use this app. I can really explore the depths of my imagination!",
        "reviewerNickname": "Juan Chavez",
        "createdDate": "2024-01-02T11:19:36-07:00",
        "territory": "USA"
      },
      "relationships": {
        "response": {
          "links": {
            "self": "https://api.appstoreconnect.apple.com/v1/customerReviews/00000028-b08c-0014-9674-c54800000000/relationships/response",
            "related": "https://api.appstoreconnect.apple.com/v1/customerReviews/00000028-b08c-0014-9674-c54800000000/response"
          }
        }
      },
      "links": {
        "self": "https://api.appstoreconnect.apple.com/v1/customerReviews/00000028-b08c-0014-9674-c54800000000"
      }
    }
  ],
  "links": {
    "self": "https://api.appstoreconnect.apple.com/v1/appStoreVersions/d716c220-3de9-4cf2-a885-8cfb43a11087/customerReviews?filter%5Bterritory%5D=USA&limit=1",
    "next": "https://api.appstoreconnect.apple.com/v1/appStoreVersions/d716c220-3de9-4cf2-a885-8cfb43a11087/customerReviews?cursor=AQ.AJJtGDc&filter%5Bterritory%5D=USA&limit=1"
  },
  "meta": {
    "paging": { "total": 10, "limit": 1 }
  }
}
```

> Note: the `response` relationship here is **links-only** — the reply text is **not**
> in this payload. See 2c for how to pull it in.

### 2b. `CustomerReview` resource

| Field | Type | Notes |
|---|---|---|
| `type` | string | Always `"customerReviews"` |
| `id` | string | Review id (UUID-style) |
| `attributes.rating` | integer | 1–5 **(from example)** |
| `attributes.title` | string | Review title |
| `attributes.body` | string | Review body |
| `attributes.reviewerNickname` | string | Reviewer display name |
| `attributes.createdDate` | string (ISO-8601 w/ offset) | e.g. `2024-01-02T11:19:36-07:00` **(from example)** |
| `attributes.territory` | string (TerritoryCode) | 3-letter, e.g. `USA` |
| `relationships.response` | to-one | Link to the developer response (see below) |

Envelope `CustomerReviewsResponse`:
| Field | Type | Notes |
|---|---|---|
| `data[]` | CustomerReview[] | The reviews |
| `included[]` | mixed | Present only when `?include=response` is used — holds the response resources |
| `links.self` / `links.next` | string | Cursor pagination (`cursor=` param) |
| `meta.paging.total` / `.limit` | integer | Paging info |

### 2c. The developer reply: `CustomerReviewResponseV1`

Fetched via `?include=response` (lands in `included[]`) or `GET /v1/customerReviews/{id}/response`.
Example shape **(from known schema — Apple's `.md` stub did not enumerate the attribute types)**:

```json
{
  "data": {
    "type": "customerReviewResponses",
    "id": "00000028-...-response-id",
    "attributes": {
      "responseBody": "Thanks so much for the kind words — happy creating!",
      "lastModifiedDate": "2024-01-03T09:10:00-07:00",
      "state": "PUBLISHED"
    },
    "relationships": {
      "review": {
        "links": {
          "self": ".../v1/customerReviewResponses/{id}/relationships/review",
          "related": ".../v1/customerReviewResponses/{id}/review"
        }
      }
    },
    "links": { "self": ".../v1/customerReviewResponses/{id}" }
  },
  "links": { "self": ".../v1/customerReviews/{reviewId}/response" }
}
```

| Field | Type | Notes |
|---|---|---|
| `type` | string | `"customerReviewResponses"` |
| `id` | string | Response id |
| `attributes.responseBody` | string | **The reply text** |
| `attributes.lastModifiedDate` | string (ISO-8601) | Last update time |
| `attributes.state` | enum | `PUBLISHED` or `PENDING_PUBLISH` |

> A review with no reply: `GET /customerReviews/{id}/response` returns no data (empty/404),
> and with `?include=response` the review's `response` relationship has no matching entry
> in `included[]`.

---

## 3. What this means for our state management

Both stores let us read back the reply, so the store is a reliable source of truth for
**"is this review replied to, and what is the reply?"** But note the differences and the
one gap:

- **Play:** reply is inline (`comments[].developerComment.text`) — cheap to check per review.
- **Apple:** reply is a separate related resource — needs `?include=response` (one extra
  query param on the list call) or a per-review fetch. Plan for the `include`.
- **Neither store records authorship** (human vs our bot). So for the RAG corpus we must
  still keep our own **source tag** (`human` / `edited` / `ai`) to know which replies are
  safe to learn from.

**We still need local state for things the store's "has a reply?" check cannot do:**
1. **Notification dedup** — reviews we deliberately *don't* reply to (e.g. below
   `AUTO_REPLY_MIN_RATING`, or support-redirect cases) would otherwise look "unreplied"
   forever and get re-processed / re-notified every run. `posted_ids` prevents that.
2. **Baseline** — on first run we baseline the existing backlog so we don't blast replies
   to old reviews.
3. **Incremental fetch boundaries** — `last_review_id` / pagination (`stop_at_boundary`)
   drive efficient incremental reads.
4. **Authorship for the corpus** — the `human`/`edited`/`ai` source tag (hash only in the
   public repo; text stays in the private store).
5. **Reply edit/prune windows** — Play replies are mutable; the store doesn't track our
   pruning policy.

**So the design:** lean on the store's developer-reply field as the *source of truth for
reply content* (we no longer need to persist reply text ourselves), but keep a slimmed
state for dedup, baseline, pagination, and the human/AI authorship tag.

---

## Sources

- [Google Play — REST Resource: reviews](https://developers.google.com/android-publisher/api-ref/rest/v3/reviews)
- [Google Play — reviews (python client, example JSON)](https://googleapis.github.io/google-api-python-client/docs/dyn/androidpublisher_v3.reviews.html)
- [Google Play — Reply to Reviews](https://developers.google.com/android-publisher/reply-to-reviews)
- [Apple — List customer reviews for an app store version](https://developer.apple.com/documentation/appstoreconnectapi/get-v1-appstoreversions-_id_-customerreviews)
- [Apple — CustomerReview.Attributes](https://developer.apple.com/documentation/appstoreconnectapi/customerreview/attributes-data.dictionary)
- [Apple — Get a customer review response](https://developer.apple.com/documentation/appstoreconnectapi/get-v1-customerreviews-_id_-response)
- [Apple — Read customer review response information](https://developer.apple.com/documentation/appstoreconnectapi/get-v1-customerreviewresponses-_id_)
- [Apple — CustomerReviewResponseV1.Attributes](https://developer.apple.com/documentation/appstoreconnectapi/customerreviewresponsev1/attributes-data.dictionary)
