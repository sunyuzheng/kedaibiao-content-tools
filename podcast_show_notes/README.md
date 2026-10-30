# Podcast-specific show notes

This directory is the versioned source of truth for episode descriptions that
intentionally differ from the corresponding YouTube description.

Editorial instructions are in [STYLE.md](STYLE.md). The exact owner-approved
evergreen intro and Ask/community/course template lives in
[PROMOTION.json](PROMOTION.json).

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

New-episode plans use the deterministic `promotion_html_v2` format for every
nonempty description source. `tools/podcast/promotion.py` puts the episode
content first, then the approved intro and footer, so podcast apps preview each
episode with its own opening (v1 put the shared intro and an episode-content
heading first; frozen v1 plans keep those bytes). It removes only exact legacy
fragments from the versioned allowlist and preserves episode resources,
chapters, dynamic tags, and specific community questions. Already-composed
approved templates are recognized without a second wrapper, including one
written with a superseded copy: the legacy config keeps each replaced copy in
`previous_promotions` only for this recognition, and such a wrapper gets the
current copy. An explicitly empty new-episode source is hash-locked as empty
and uses the locked episode title for its body, with an `empty_source` quality
warning. Invalid sources and missing or changed renderer inputs stay fail-closed.

Each immutable payload locks the source hash, config hashes, fallback title,
and rendered hash. The unattended gate and executor reproduce those same
bytes and refuse missing or changed config. The config hashes are also pinned
in code, so changing the public template requires a reviewed config/code
update. Config comes from the pinned source checkout, not a runtime bundle.

Existing `portable_html_v1` plans retain their original meaning and need no
promotion config. Published-episode sidecar maintenance continues to use v1
and its separate description approval scope; selecting the new renderer does
not automatically apply a historical refresh. Published descriptions get a new
copy only through `tools/podcast/reorder_show_notes_promotion.py`, as its own
reviewed description scope. The technical format described in STYLE.md remains
the portable body renderer inside the new wrapper.
