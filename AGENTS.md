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

Only publish automatically when all are true:

- YouTube privacy is `public`.
- Content class is `normal_video`.
- Transistor status is not already `published`.

Do not automatically publish unlisted, private, member/course/internal/demo videos, or live replays.

Use transcript status to prioritize subtitle cleanup and report quality, not to exclude otherwise eligible public normal videos from Transistor.

All scheduled runs are plan-only. Any Transistor mutation must use an immutable plan and its exact reviewed approval hash.

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
