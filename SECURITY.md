# Security and privacy

Run locally using developer applications you control. Explicit `auth` and `export-spotify` commands open the providers' authorization flows. Browser credentials/cookies are never extracted. No telemetry or transfer-service backend is included.

Tokens and library checkpoints default outside the checkout, under `~/.local/share/spotify-to-youtube/`. New directories use owner-only permissions; atomic checkpoint files use mode 0600. The CLI sets a restrictive umask. Existing directories may have broader permissions; secure them yourself. Tokens are plaintext locally, not encrypted. Protect your OS account and backups.

Search caches contain personal queries and result metadata. Keep the entire state directory private. API diagnostics retain only bounded, allowlisted reason codes, HTTP status, parsed Retry-After values, and local cooldown fields. Raw response bodies, arbitrary error messages, request URLs, and authorization headers are excluded. OAuth authorization URLs are not printed. Cooldowns survive restarts; cached searches and write reconciliation reduce repeat requests without bypassing provider limits.

Never commit OAuth client files, tokens, library exports, screenshots, real-user fixtures, or private checkpoint data. `.gitignore` is a convenience, not an access-control boundary. Keep private data outside the checkout and inspect staged files before every public push. Avoid raw API exceptions or authorization URLs in issue reports. Revoke compromised grants in your provider account settings and delete local cached tokens.

Only private playlists owned by the expected channel are accepted. Remote writes require `--execute`. The YouTube OAuth scope is broader than this tool's operations; review source and consent before granting it. Imported approvals are trusted local input and should be inspected before applying.

Dependencies come from PyPI. Use a virtual environment and your dependency review process. Report vulnerabilities through private security reporting if enabled on the repository, or contact the maintainer privately. Never include live credentials in reports.
