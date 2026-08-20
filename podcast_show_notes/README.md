# Podcast-specific show notes

This directory is the versioned source of truth for episode descriptions that
intentionally differ from the corresponding YouTube description.

- File name: `<youtube_video_id>.txt`
- Encoding: UTF-8 plain text
- Maximum length: 10,000 characters
- Transistor placeholders such as `{{video}}`, `{{transcript}}`, and
  `{{people}}` are allowed and validated.
- Timestamps must refer to the podcast audio, not a longer YouTube cut, and must
  be strictly increasing.

The sync planner prefers this directory over an archive-local
`*.podcast-description.txt`, then falls back to `.description` and the fresh
YouTube snapshot. A versioned file can update an existing published episode
only through the dedicated description plan and its exact approval hash.
