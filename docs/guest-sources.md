# Guest interview sources

`guests.json` remains the only authoritative guest roster, identity, ordering, and interview-membership file. The website imports it into generated deployment snapshots; no separate hand-maintained roster is introduced.

Existing entries default to `primary_source_type: "youtube"` when that field is absent. Their fields, ordering, primary videos and URLs remain unchanged. YouTube records continue to require matching `all_video_ids` and `all_urls`, with video metadata in `guest_video_metadata.json`.

## Circle-only interview

A Circle-only guest explicitly supplies:

| Field | Meaning |
|---|---|
| `primary_source_type` | `circle` |
| `primary_video_id` | `null`; never a made-up YouTube ID |
| `all_video_ids` | `[]`; video pipelines have no YouTube item to process |
| `primary_url` | Verified `https://www.superlinear.academy/c/recording/...` URL |
| `all_urls` | One-element array containing that same real URL |
| `episode_count` | `1`; one interview, despite zero YouTube videos |
| `max_views` | `null`; unavailable is not zero |
| `slug` | Explicit stable guest-page slug |
| `primary_source_title` | Actual Circle post title |
| `primary_source_published_at` | Actual Circle post publication timestamp |
| `interview_date` | Separately verified recording date, `YYYY-MM-DD` |
| `thumbnail_url` | Root-relative public website asset under `/guest-media/` |
| `guest_bio`, `guest_bio_en` | Optional source-backed biography, using interview-time framing where needed |

The initial entry is Jinjing Liang, `/guests/jinjing-liang`. Its source is Circle post `35652694`, recorded August 19, 2026 and published August 20, 2026. These dates describe different events. The thumbnail is an unchanged still at 00:30:00 from the original interview, copied to the website’s public assets. It is not a generated portrait.

The validator checks Circle-specific nulls, source URL, title, dates and thumbnail path. The video metadata builder explicitly skips non-YouTube sources; other video operations iterate `all_video_ids`, which is empty. The existing 365 YouTube metadata entries remain unchanged.

The website models this as a Circle episode with a nullable video ID. It shows the real source title, recording date, companion publication date and a Superlinear CTA. It emits a `CreativeWork` source link, not a made-up `VideoObject`, upload identity, embed or viewing count. A root-relative thumbnail becomes an absolute `www.lizheng.ai` URL in SEO metadata.

## Review and release

Local isolated worktrees may be synchronized by setting `KEDAIBIAO_CHANNEL_DIR` to this content worktree before running the website’s `pnpm sync:guest-video-metadata`. This generates the deployment snapshots. Run the upstream validator and website typecheck/build before review.

Publication requires explicit approval of the exact changes and destinations. After that approval, publish the authoritative content change and website change together, including the thumbnail asset. Do not point production at an unpublished local draft. Pending English articles remain in opt-in local preview until their actual Substack publication is verified.

See [guest-insights-workflow.md](guest-insights-workflow.md) for the full editorial and distribution process: Yuzheng posts the insights, tags verified guests, and personally invites them to repost the original social post.
