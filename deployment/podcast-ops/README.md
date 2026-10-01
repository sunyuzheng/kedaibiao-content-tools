# Private podcast operations repository

Target: `sunyuzheng/kedaibiao-podcast-ops` (private, main). This directory is a
reviewable template; committing it to the public source repository does not
activate the operational schedule.

Seed files generated locally and copied only to the private repository:

- `code-version.txt`: full 40-character commit of the public application source.
- `runtime/state.json`: minimum historical dates/metadata, pending execution and receipts.
- `podcast_series.json`: canonical cloud series assignments after the approved cutover.

The workflow runs at minute 17 every two hours (UTC) and supports manual dispatch.
`plan` is the manual default. Scheduled `auto` follows `publication-policy.json`;
with the shipped `enabled: false`, it only prepares plans. `publish` requires a
successful producer run, exact plan and scope hashes, unexpired evidence, and
`local_writer_disabled: true`. A hash verifies the payload, not the user's consent.

Only the repository secret `TRANSISTOR_API_KEY` is required. The GitHub token is
scoped to this private repository. No YouTube cookies, personal OAuth tokens,
Resend credentials, or local `.env` file belong here.

See the public source runbook `docs/GitHub-Actions播客同步运行手册.md` and Dot handoff
`docs/Transistor云端同步与Dot交接方案.md`. Never make this repository public: Git
history and artifacts contain private operational metadata and current media.
