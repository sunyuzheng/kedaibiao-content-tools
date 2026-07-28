# YouTube to Podcast

Turn a public YouTube channel into a podcast without turning historical cleanup
into accidental new releases.

YouTube to Podcast is a local, plan-first command-line tool. Version 0.1 uses
Transistor as its first podcast-host adapter. It discovers public YouTube
videos, prepares MP3 audio and SRT/VTT transcripts with `yt-dlp`, compares them
with a Transistor show, and produces an immutable review plan before any remote
write is allowed.

## Safety model

- `plan` is read-only with respect to Transistor.
- `apply` requires the approval hash printed by that exact plan.
- Any edit to the actions, media, transcript, configuration, or remote
  preconditions makes execution fail closed.
- The default is `incremental + draft`, with at most three actions per plan.
- Historical gaps are quarantined in incremental mode.
- Backfill mode can create drafts, but cannot publish.
- Original YouTube upload dates are preserved when publishing.
- New episodes are created oldest first and their assigned numbers are read back.
- Existing published episodes are never reordered or deleted.
- Existing drafts are surfaced for manual review and never reused automatically.
- Scheduled jobs should run `plan`, never `apply`.

## Requirements

- Python 3.11 or newer
- `ffmpeg`
- A Transistor API key and numeric show ID
- A public YouTube channel `/videos` URL

## Install for local evaluation

```bash
git clone https://github.com/sunyuzheng/kedaibiao-content-tools.git
cd kedaibiao-content-tools
python3 -m venv .venv-youtube-to-podcast
. .venv-youtube-to-podcast/bin/activate
pip install -e packages/youtube-to-podcast
```

Install `ffmpeg` with your operating system's package manager. Because YouTube
changes frequently, upgrade `yt-dlp` explicitly before a run:

```bash
youtube-to-podcast update-ytdlp --pre
```

The tool never silently upgrades itself during `plan` or `apply`.

## Quick start

Create a clean project directory:

```bash
mkdir my-podcast-sync
cd my-podcast-sync

youtube-to-podcast init \
  --channel-url https://www.youtube.com/@YOUR_CHANNEL/videos \
  --show-id YOUR_NUMERIC_TRANSISTOR_SHOW_ID
```

`init` also creates a `.gitignore` that excludes `.env` and runtime media.
Copy `.env.example` to `.env`, then add the secret locally:

```dotenv
TRANSISTOR_API_KEY=replace_me
```

Check the environment and make a plan:

```bash
youtube-to-podcast doctor --online
youtube-to-podcast plan
```

Review:

```text
.youtube-to-podcast/plans/latest.md
.youtube-to-podcast/plans/latest.html
```

`plan` creates both files. Open the interactive local review again at any time:

```bash
youtube-to-podcast review
```

If the plan is correct, apply only that exact scope:

```bash
youtube-to-podcast apply \
  --plan .youtube-to-podcast/plans/latest.json \
  --approval-hash HASH_PRINTED_BY_PLAN
```

The default creates drafts only. To allow newly discovered videos to publish
with their original YouTube dates, change `publication = "publish"` and build a
new plan. Do not reuse an old approval hash after changing configuration.

## Configuration

`youtube-to-podcast.toml`:

```toml
schema_version = 1
work_dir = ".youtube-to-podcast"

[youtube]
channel_url = "https://www.youtube.com/@YOUR_CHANNEL/videos"

[transistor]
show_id = "12345"

[policy]
mode = "incremental"
publication = "draft"
max_actions = 3
max_candidates = 12
include_live = false
download_subtitles = true
subtitle_languages = ["en.*", "zh.*"]
```

Modes:

- `incremental`: only considers videos newer than the newest published
  Transistor episode linked by `video_url`. Older missing items are reported as
  historical gaps.
- `backfill`: considers historical gaps, but only as drafts. Review and publish
  them manually or return to an incremental publish plan after establishing a
  trusted baseline.

`max_actions` limits remote mutations in one plan. `max_candidates` separately
bounds YouTube metadata checks, so one blocked candidate does not permanently
starve later valid videos.

## Transcripts

Human and automatic YouTube subtitles are downloaded when available. SRT is
preferred over VTT, and obvious automatic-caption filenames receive lower
priority. The selected timed-text file stays local; clean plain text is sent to
Transistor as `transcript_text`.

A missing transcript or description is a visible warning, not an automatic
block. Missing title, publish date, audio, non-public availability, duplicate
remote matches, an existing draft, or unexpected live content blocks the item.

## Weekly automation

A safe weekly scheduler runs only:

```bash
youtube-to-podcast update-ytdlp --pre
youtube-to-podcast doctor --online
youtube-to-podcast plan
```

It may notify a human with `latest.md`. It must not run `apply`, copy an approval
hash automatically, or publish without review.

## Non-goals in 0.1

- Private, members-only, age-gated, or cookie-authenticated YouTube content
- Automatic deletion, duplicate cleanup, or reordering of existing episodes
- Automatic repair or publishing of pre-existing drafts
- Fully unattended publishing
- Podcast hosts other than Transistor
- A hosted service that stores user API keys

See [SECURITY.md](SECURITY.md) before using real credentials and
[CONTRIBUTING.md](CONTRIBUTING.md) before proposing changes.
