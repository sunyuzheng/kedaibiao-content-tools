# Contributing

YouTube to Podcast is intentionally conservative: a failed plan is preferable
to a wrong public episode.

Before opening a pull request:

```bash
python3 -m unittest discover -s packages/youtube-to-podcast/tests -v
python3 -m compileall -q packages/youtube-to-podcast/src
```

Changes that add a remote mutation must include:

- a read-only planning representation;
- exact local and remote preconditions;
- inclusion in the approval hash;
- readback verification;
- a regression test showing that stale or tampered input fails closed.

Never add real channel snapshots, show IDs, episode IDs, API keys, signed upload
URLs, downloaded media, or transcripts to test fixtures.
