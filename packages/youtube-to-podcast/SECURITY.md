# Security

## Secrets

Use the `TRANSISTOR_API_KEY` environment variable or a local `.env` file next
to `youtube-to-podcast.toml`. Never put a real key in configuration, plans,
logs, screenshots, issues, or commits.

Plans contain episode titles, descriptions, YouTube URLs, local relative file
paths, content hashes, and remote episode IDs. Treat plans and ledgers as
operational data even though they do not contain the API key.

## Execution boundary

`apply` accepts only the approval hash derived from the exact action scope.
Before its first remote write it rechecks the plan hash, approval scope,
configuration, every local audio/transcript hash, and every remote episode
precondition.

Do not automate approval-hash extraction or schedule `apply`. Doing so removes
the human review boundary this project is designed to preserve.

## Reporting a vulnerability

Do not open a public issue containing credentials or private channel data.
Contact the repository owner privately with a minimal reproduction and redact
all tokens, signed upload URLs, and personal media.
