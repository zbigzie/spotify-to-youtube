# Spotify to YouTube

A local Python CLI that helps move **Spotify liked songs** into a **private YouTube playlist**. Export your library or import a JSON list, review recording matches, then add videos in resumable batches.

**Early release:** offline recovery and matching tests pass. Live OAuth and end-to-end API transfer have not been verified for this release. Users supply their own developer applications. Matching uses metadata, not audio fingerprints.

## Features

- Read-only Spotify export with pagination and page checkpoints.
- Local imports, saved search candidates, manual approval, and optional strict matching against explicitly trusted channels.
- Private playlists, account/ownership checks, duplicate prevention, and verification after upload batches.
- Atomic checkpoints and recovery after dropped connections. Unknown write outcomes stop for reconciliation rather than retrying automatically.
- No hosted transfer service, telemetry, browser-cookie extraction, Spotify modifications, or deletion of YouTube content.

## Install

Python 3.10+ on macOS or Linux is required. Native Windows is unsupported; use WSL.

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
spotify-to-youtube --help
```

State and tokens default to `~/.local/share/spotify-to-youtube/`, outside the repository. To run a separate transfer, place `--state /path/to/private/directory` **before** the subcommand. One state directory belongs to one source snapshot and one destination. Keep it private.

## 1. Get your songs

### Import an existing list

No Spotify authorization is needed for import. Save a JSON array outside the checkout:

```json
[
  {
    "title": "Example Song - Live",
    "artists": ["Example Artist"],
    "album": "Example Live Album",
    "duration": "3:42"
  }
]
```

```sh
spotify-to-youtube import /path/to/private/songs.json
```

`title` and `artists` are required; album and duration improve matching. An optional Boolean `explicit` flag is retained for review. Browser-ledger `raw` text can also supply the explicit marker as a standalone `E` line.

The importer accepts a browser-transfer ledger with `youtubeUrl` and status `added`, `duplicate`, `matched`, or `approved`. Completed entries retain their status; other matches become local approvals. `apply` reconciles them against the destination. If a previously completed video is absent, the tool stops for inspection instead of silently inserting it again. Import only trusted files. Completeness of an imported list is the caller's responsibility.

### Or export Spotify liked songs

Create your own app in the [Spotify Developer Dashboard](https://developer.spotify.com/dashboard), with redirect URI exactly `http://127.0.0.1:8888/callback`. Spotify's app/account eligibility and development-mode restrictions apply; this tool does not bypass them.

```sh
spotify-to-youtube export-spotify --client-id YOUR_SPOTIFY_CLIENT_ID
```

This explicitly opens PKCE authorization for `user-library-read` and `user-read-private`. No Spotify client secret is needed. Check the printed Spotify account ID. The export resumes at its saved page. Avoid changing your likes during export: count changes are detected, but same-count replacements cannot reliably be detected by offset pagination. Use a new state directory for a fresh snapshot.

## 2. Authorize Google

In [Google Cloud Console](https://console.cloud.google.com/), create a project, enable **YouTube Data API v3**, configure OAuth consent, and create an OAuth client of type **Desktop app**. Add your Google account as a test user if the app is in testing mode. Download the client JSON to a private directory outside this checkout.

```sh
spotify-to-youtube auth --client-secrets /path/to/private/client_secret.json
spotify-to-youtube account
```

Check the channel name and copy its ID for `--channel`. Choose the correct channel if you use a Brand Account. OAuth uses a temporary loopback callback; the refresh token is stored locally. Testing-mode grants may expire, requiring `auth` again.

Google requires a broad YouTube write scope, `youtube.force-ssl`, for playlist insertion. Its consent screen describes capabilities beyond the subset used here. This tool only reads channels/playlists/videos and creates playlists/inserts playlist items. There is no playlist-only OAuth grant. Review the consent screen before authorizing.

Keep credentials out of chat, screenshots, issue reports, and Git. If Google shows first-use terms, review them yourself. A Google Cloud free-trial or billing signup is not part of this tool's authorization flow; stop if setup unexpectedly asks for payment details.

## 3. Search and approve recordings

```sh
spotify-to-youtube search --limit 25
spotify-to-youtube review 1
spotify-to-youtube approve 1 VIDEO_ID --note "Checked artist, album, duration, and live recording"
```

Search saves up to eight candidates per song; it never approves or uploads them. Inspect candidate URLs and channel ownership. Empty results remain `uncertain`, not `unavailable`. Retry an unresolved track with `search --position 1`. Record a researched exception with `unavailable 1 --note "reason"`.

For artist/label channels you independently verified, pass `--trusted-channel CHANNEL_ID` (repeat for more). Strict recommendations require a trusted channel, exact normalized title including recording/version qualifiers, all artist credits and album in metadata, and duration within two seconds. A unique strict candidate can be approved in bulk:

```sh
spotify-to-youtube search --limit 25 --trusted-channel VERIFIED_CHANNEL_ID
spotify-to-youtube approve-strict
spotify-to-youtube approve-strict --execute
```

The first approval command previews; the second changes local approvals. Metadata can be wrong. Review remixes, live versions, remasters, and clean/explicit edits carefully. If metadata omits a clean edit, the tool cannot reliably distinguish it. “Official” in a title alone never establishes a trusted source.

## 4. Select or create a private playlist

Pass an existing private playlist URL to `apply`, or create **Spotify Liked Songs**:

```sh
spotify-to-youtube create-playlist --channel YOUR_CHANNEL_ID
spotify-to-youtube create-playlist --channel YOUR_CHANNEL_ID --execute
```

The first previews; the second creates and prints the playlist URL. A recovery marker in the private description identifies a playlist after an interrupted creation response. Once bound, state refuses another destination.

## 5. Preview, upload, and resume

```sh
spotify-to-youtube apply --playlist YOUR_PLAYLIST_URL --channel YOUR_CHANNEL_ID --limit 25
spotify-to-youtube apply --playlist YOUR_PLAYLIST_URL --channel YOUR_CHANNEL_ID --limit 25 --execute
spotify-to-youtube status
```

The first command changes no remote data. Repeat the second for later batches. Each run reads all destination pages before adding approved matches and verifies membership afterward. Multiple source tracks mapped to the same video produce one playlist entry. Songs added later are appended: resolving gaps later does not restore global source order.

If an interrupted insertion remains absent, the tool stops. Inspect YouTube, allow time for updates, then explicitly retry the reported position with `apply ... --execute --retry-position N` if appropriate. If creation's outcome is unknown, inspect YouTube and supply the existing URL to `apply`. It will not blindly create a second playlist.

Use one process/state directory per playlist and avoid simultaneous editing elsewhere. Duplicate protection is not a server-side transaction. State is locked locally while a command runs. Do not edit state by hand.

`status` shows source total, captured count, matched source entries, per-status counts, and last verification. `added` counts matched unique videos; `duplicate` counts extra source entries sharing them. A command succeeding is not proof of a complete transfer. Completion requires a complete source, no pending/uncertain/approved/inflight entries, and verified destination membership; unavailable tracks remain documented exceptions.

## Quotas and failures

YouTube API quotas limit both search and insertion. Large libraries may require multiple quota windows. Check your project's quota console and current [search limits](https://developers.google.com/youtube/v3/docs/search/list) and [insertion cost](https://developers.google.com/youtube/v3/docs/playlistItems/insert). The tool does not rotate projects or evade quotas. API failures stop with saved progress and no automatic write retry. Resolve connectivity, wait for quota reset, or renew authorization, then resume.

## Development

```sh
python -m unittest discover -s tests -v
```

Tests use synthetic songs and fake APIs, with no credentials or network. Before publishing, inspect `git diff --cached` and `git ls-files`; never include real library files or tokens. See [SECURITY.md](SECURITY.md). Licensed under [MIT](LICENSE). Not affiliated with Spotify or YouTube. This transfers playlist references, not audio files.

## Troubleshooting

| Message or symptom | Next step |
| --- | --- |
| Run auth with your own Google Desktop OAuth client first | Run `auth --client-secrets` with your local client JSON, then verify the channel with `account`. Browser sign-in alone does not authorize this CLI. |
| Account differs or playlist belongs to another channel | Check `account`, the selected Brand Account, and the destination URL. Do not create another playlist to work around an account mismatch. |
| Unknown insert outcome | Keep the state directory. Run a dry run to reconcile; use `--retry-position` only after confirming the video is absent. |
| HTTP 403 or quota error | Inspect the project's API quota and authorization. A 403 alone does not identify its cause. Resume after the underlying issue is resolved. |
| Browser tab crashes during setup | Reload that tab or compare the official URL in another browser. A browser crash does not prove the API or your network is down. |

When asking for help, share the command name, error type/HTTP code, and whether it was a dry run. Redact personal song lists, channel identifiers, OAuth URLs, and credential files.

References: [Spotify saved tracks](https://developer.spotify.com/documentation/web-api/reference/get-users-saved-tracks), [Spotipy PKCE](https://spotipy.readthedocs.io/en/latest/#spotipy.oauth2.SpotifyPKCE), [Google desktop OAuth](https://developers.google.com/identity/protocols/oauth2/native-app).
