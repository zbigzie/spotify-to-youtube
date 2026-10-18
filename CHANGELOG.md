# Changelog

## 0.2.0

Transfers now stop with a durable cooldown when YouTube limits requests. Restarting preserves exponential backoff, and a valid Retry-After can extend the wait. An HTTP 429 alone is not classified as daily quota exhaustion.

- Save allowlisted error diagnostics without raw API messages, request URLs, or credentials.
- Distinguish confirmed quota exhaustion, rate limits, server errors, and authorization failures.
- Cache searches for 24 hours and recover partial searches without repeating successful search requests.
- Pace API calls and disable SDK retries; resume explicitly after a failure.
- Handle Ctrl-C while retaining checkpoints and unknown-write reconciliation.
- Preserve imported completed entries and explicit flags for recording review.
- Add offline regression coverage for repeated limits, restart recovery, malformed headers, cache expiry, interrupted writes, and secret redaction.

Google OAuth, playlist insertion, and destination verification have been exercised live. Rate-limit edge cases and Spotify export are tested with synthetic offline fixtures. This remains an early release; recording matches require review.
