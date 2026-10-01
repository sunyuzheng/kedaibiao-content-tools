# Kedaibiao Channel Project Rules

This project manages the local archive for the YouTube channel "课代表立正" and its Transistor podcast sync.

## First Read

- Read `docs/媒体库维护规则.md` before changing podcast, subtitle, transcript, or archive logic.
- Use `README.md` for the broad project map.
- Use `logs/library_manifest/library_audit.md` only as the latest generated audit output; regenerate it when in doubt.

## Source of Truth

- Do not use `archive/有人工字幕` or `archive/无人工字幕` as business truth. Those are historical storage locations only.
- Build the canonical status with:

```bash
python3 tools/check/build_library_manifest.py
```

- Canonical output:
  - `logs/library_manifest/library_manifest.json`
  - `logs/library_manifest/library_audit.md`

## Subtitle and Transcript Semantics

- True human subtitles come from `info.json.subtitles`, usually `zh` or `en`.
- Local corrected transcripts are `.corrected.srt`.
- Local uncorrected ASR is `.qwen.srt`.
- Plain `.srt` / `.vtt` files may be downloaded subtitles or older local artifacts; treat them conservatively unless metadata proves the source.
- Subtitle/transcript quality is an audit and post-production field, not a hard publishing gate.
- The current workflow deterministically converts the best local SRT/VTT source to plain text and writes it through Transistor's public `transcript_text` field. The public API does not preserve the original cue timestamps, so local timed files remain canonical.
- Never overwrite an existing remote transcript when the public API exposes only a transcript URL and the source text cannot be compared.

## Transistor Sync Policy

Title series assignments live in `podcast_series.json`; follow `docs/播客系列与编号.md`. Titles use separately numbered `对话 001｜…` / `立正说 001｜…`; RSS `episode.number` remains the global number. Missing classification blocks publishing. Never automatically sweep historical title migrations into a new-episode run.

Only publish automatically when all are true:

- YouTube privacy is `public`.
- Content class is `normal_video`.
- Transistor status is not already `published`.

Do not automatically publish unlisted, private, member/course/internal/demo videos, or live replays.

Use transcript status to prioritize subtitle cleanup and report quality, not to exclude otherwise eligible public normal videos from Transistor.

The scheduled task may automatically publish at most three genuinely new episodes when every strict gate passes: immutable plan and publish-scope hashes are valid; fresh public and per-candidate YouTube evidence agree; canonical policy is `public + normal_video + ready_public_normal`; each candidate is ahead of the latest published playlist baseline; audio, date, title, YouTube identity, description, local hashes, and remote preconditions are complete; no global publish blocker exists; and every projected reorder action belongs to a newly published episode. Unknown warnings fail closed. Missing transcripts and explicitly allowlisted Show Notes quality warnings are non-blocking receipt items.

The executor's existing scope hash remains an integrity lock, not a claim of per-run human approval. Existing-episode Show Notes/transcript updates, historical backfills, duplicate-draft cleanup, and any change to a historical episode number/title remain separate reviewable scopes and must never be swept into scheduled auto-publish.

All plain-text descriptions, including the YouTube/local fallback, must use the deterministic `portable_html_v1` renderer before publication so RSS clients do not collapse raw newlines. A versioned `podcast_show_notes/<video_id>.txt` sidecar remains the preferred high-quality source; its absence alone does not block a new episode.

Scheduled owner notifications send completion receipts only after all post-publish checks pass. New or changed real blockers must include human context, impact, exact action, and location. Maintenance uses a separate fingerprint. Quarantined historical gaps and unchanged blockers stay silent.

After any Transistor publish, run:

```bash
.venv-podcast/bin/python tools/check/check_upload_quality.py --n 500
.venv-podcast/bin/python tools/upload/reorder_episodes_by_date.py
.venv-podcast/bin/python tools/check/build_podcast_sync_plan.py
```

`reorder_episodes_by_date.py` defaults to plan-only and must fail closed if any published episode lacks a local date. Applying its plan also requires the exact reviewed approval hash.

## Current Maintenance Direction

- Prefer fixing tools to read manifest semantics before moving archive folders.
- Keep generated audits and logs under `logs/`; do not commit them unless explicitly requested.
- Never persist API keys, OAuth tokens, or Transistor credentials in scripts, docs, logs, or committed files.

## Cloud entry point

The private GitHub Actions entry is `tools/automation/cloud_podcast.py`; it defaults
to plan-only. Use `docs/GitHub-Actions播客同步运行手册.md` for setup/cutover and
`docs/Transistor云端同步与Dot交接方案.md` for Dot responsibilities. The template under
`deployment/podcast-ops/` is deployed only to the private operations repository.
After an approved cutover, that repository's `podcast_series.json` owns new
assignments; the public catalog remains a migration baseline. Never run both
local and cloud publication writers concurrently.
