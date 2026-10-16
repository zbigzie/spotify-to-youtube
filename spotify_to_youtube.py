"""Local playlist transfer. No browser cookies, hosted services, or Spotify writes."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import html
import json
import os
from pathlib import Path
import re
import sys
import tempfile
import unicodedata
from urllib.parse import parse_qs, urlparse
import uuid

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
DEFAULT_STATE = Path.home() / ".local/share/spotify-to-youtube"


class StopTransfer(Exception):
    """An actionable stop that preserves the checkpoint."""


def now():
    return datetime.now(timezone.utc).isoformat()


def save_json(path, data):
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=".checkpoint-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as out:
            json.dump(data, out, ensure_ascii=False, indent=2)
            out.write("\n")
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextmanager
def locked(directory):
    import fcntl  # macOS/Linux; native Windows is not supported.
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (directory / ".lock").open("a") as stream:
        try:
            fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise StopTransfer("Another process is using this state directory.") from exc
        yield


class State:
    def __init__(self, directory):
        self.path = Path(directory) / "state.json"
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {
            "schema": 1, "tracks": [], "destination": None}
        if self.data.get("schema") != 1:
            raise StopTransfer("Unsupported checkpoint schema.")

    def save(self):
        self.data["updated_at"] = now()
        save_json(self.path, self.data)


def normalize(value):
    value = unicodedata.normalize("NFKD", html.unescape(value)).casefold()
    value = "".join(c for c in value if not unicodedata.combining(c))
    return " ".join(re.findall(r"\w+", value.replace("&", " and ")))


def seconds(value):
    if isinstance(value, (int, float)):
        return float(value)
    if not value:
        return None
    if re.fullmatch(r"\d+(?::\d+){1,2}", value):
        result = 0
        for part in value.split(":"):
            result = result * 60 + int(part)
        return result
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if match:
        h, m, s = [int(x or 0) for x in match.groups()]
        return h * 3600 + m * 60 + s
    raise StopTransfer("Invalid duration in source or API response.")


def youtube_id(value, playlist=False):
    value = value.strip()
    if "://" in value:
        url = urlparse(value)
        if url.hostname not in {"youtube.com", "www.youtube.com", "music.youtube.com", "youtu.be"}:
            raise StopTransfer("Expected a YouTube URL or ID.")
        value = (url.path.strip("/") if url.hostname == "youtu.be" and not playlist else
                 parse_qs(url.query).get("list" if playlist else "v", [""])[0])
    if not re.fullmatch(r"[A-Za-z0-9_-]+", value) or (not playlist and len(value) != 11):
        raise StopTransfer("Invalid playlist/video ID.")
    return value


def import_tracks(state, rows):
    if state.data["tracks"] or state.data.get("spotify_export"):
        raise StopTransfer("Source already exists. Use another --state directory.")
    if not isinstance(rows, list) or not rows:
        raise StopTransfer("Expected a nonempty JSON array.")
    tracks = []
    for position, row in enumerate(rows, 1):
        if not row.get("title") or not isinstance(row.get("artists"), list) or not row["artists"]:
            raise StopTransfer(f"Track {position} needs a title and artists array.")
        track = {"position": position, "title": row["title"], "artists": row["artists"],
                 "album": row.get("album", ""), "duration": seconds(row.get("duration")),
                 "spotify_url": row.get("spotifyUrl", ""), "status": "pending", "candidates": []}
        if isinstance(row.get("explicit"), bool):
            track["explicit"] = row["explicit"]
        elif isinstance(row.get("raw"), str):
            track["explicit"] = "E" in row["raw"].splitlines()
        if row.get("youtubeUrl") and row.get("status") in {"added", "duplicate", "matched", "approved"}:
            status = row["status"] if row["status"] in {"added", "duplicate"} else "approved"
            track.update(video_id=youtube_id(row["youtubeUrl"]), status=status, approval="imported match")
        tracks.append(track)
    state.data.update(tracks=tracks, source_total=len(tracks), source_complete=True)
    state.save()


def assess(track, candidate, trusted):
    """Metadata recommendation, never proof of identical audio or automatic approval."""
    reasons = []
    if candidate["channel_id"] not in trusted:
        reasons.append("channel not explicitly trusted")
    title = re.sub(r"\s*[\[(](?:official audio|audio)[\])]\s*", " ", candidate["title"], flags=re.I)
    title = normalize(title)
    expected = normalize(track["title"])
    if title not in {expected, normalize(track["artists"][0]) + " " + expected}:
        reasons.append("title or recording/version differs")
    text = " " + normalize(candidate["title"] + " " + candidate["description"] + " " + candidate["channel"]) + " "
    if not all(" " + normalize(a) + " " in text for a in track["artists"]):
        reasons.append("artist credits incomplete")
    if not track["album"] or " " + normalize(track["album"]) + " " not in text:
        reasons.append("album not corroborated")
    if track["duration"] is None or candidate["duration"] is None or abs(track["duration"] - candidate["duration"]) > 2:
        reasons.append("duration differs or is missing")
    return dict(candidate, recommendation="review" if reasons else "strict", reasons=reasons)


class YouTube:
    def __init__(self, directory, client_file=None):
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        token = Path(directory) / "youtube-token.json"
        if client_file:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
            credentials = flow.run_local_server(host="127.0.0.1", port=0, timeout_seconds=180)
        else:
            if not token.exists():
                raise StopTransfer("Run auth with your own Google Desktop OAuth client first.")
            credentials = Credentials.from_authorized_user_file(str(token), SCOPES)
            if not credentials.valid:
                if not credentials.refresh_token:
                    raise StopTransfer("Google authorization expired. Run auth again.")
                credentials.refresh(Request())
        save_json(token, json.loads(credentials.to_json()))
        self.api = build("youtube", "v3", credentials=credentials, cache_discovery=False)

    def call(self, resource, method="list", **kwargs):
        return getattr(getattr(self.api, resource)(), method)(**kwargs).execute(num_retries=0)

    def pages(self, resource, **kwargs):
        while True:
            page = self.call(resource, **kwargs)
            yield from page.get("items", [])
            if not page.get("nextPageToken"):
                return
            kwargs["pageToken"] = page["nextPageToken"]

    def accounts(self):
        return list(self.pages("channels", part="snippet", mine=True, maxResults=50))

    def verify_account(self, expected):
        if expected not in {x["id"] for x in self.accounts()}:
            raise StopTransfer("YouTube account differs from --channel. Run account.")

    def verify_playlist(self, playlist, channel):
        self.verify_account(channel)
        rows = self.call("playlists", part="snippet,status", id=playlist).get("items", [])
        if len(rows) != 1 or rows[0]["snippet"]["channelId"] != channel:
            raise StopTransfer("Playlist not found or owned by another channel.")
        if rows[0]["status"]["privacyStatus"] != "private":
            raise StopTransfer("Destination must already be private.")

    def members(self, playlist):
        return {x["snippet"]["resourceId"]["videoId"] for x in self.pages(
            "playlistItems", part="snippet", playlistId=playlist, maxResults=50)
            if x.get("snippet", {}).get("resourceId", {}).get("videoId")}

    def search(self, query):
        results = self.call("search", part="snippet", type="video", q=query, maxResults=8, safeSearch="none")
        ids = [x["id"]["videoId"] for x in results.get("items", [])]
        if not ids:
            return []
        details = self.call("videos", part="snippet,contentDetails", id=",".join(ids))
        rows = []
        for item in details.get("items", []):
            s = item["snippet"]
            rows.append({"id": item["id"], "title": html.unescape(s["title"]), "channel": s["channelTitle"],
                "channel_id": s["channelId"], "description": s.get("description", ""),
                "duration": seconds(item["contentDetails"].get("duration")),
                "url": "https://www.youtube.com/watch?v=" + item["id"]})
        return rows

    def insert(self, playlist, video):
        return self.call("playlistItems", "insert", part="snippet", body={"snippet": {
            "playlistId": playlist, "resourceId": {"kind": "youtube#video", "videoId": video}}})


def bind(state, playlist, channel):
    destination = {"playlist_id": playlist, "channel_id": channel}
    if state.data.get("destination") and state.data["destination"] != destination:
        raise StopTransfer("Checkpoint belongs to another destination.")
    state.data["destination"] = destination
    state.save()


def create_playlist(state, api, channel):
    api.verify_account(channel)
    if state.data.get("destination"):
        raise StopTransfer("Destination already exists. Continue with apply.")
    attempt = state.data.setdefault("create_attempt", {
        "marker": "spotify-to-youtube:" + str(uuid.uuid4()), "channel": channel, "sent": False})
    if attempt["channel"] != channel:
        raise StopTransfer("Creation checkpoint belongs to another channel.")
    matches = [p for p in api.pages("playlists", part="snippet,status", mine=True, maxResults=50)
               if attempt["marker"] in p["snippet"].get("description", "")]
    if len(matches) > 1:
        raise StopTransfer("Multiple recovery playlists found. Inspect before binding manually.")
    if matches:
        playlist = matches[0]["id"]
    else:
        if attempt["sent"]:
            raise StopTransfer("Creation outcome unknown. Inspect YouTube and pass the playlist URL to apply. No duplicate created.")
        attempt["sent"] = True
        state.save()
        playlist = api.call("playlists", "insert", part="snippet,status", body={
            "snippet": {"title": "Spotify Liked Songs", "description": "Local transfer. " + attempt["marker"]},
            "status": {"privacyStatus": "private"}})["id"]
    bind(state, playlist, channel)
    api.verify_playlist(playlist, channel)
    return playlist


def get_track(state, position):
    if position < 1 or position > len(state.data["tracks"]):
        raise StopTransfer("Track position is out of range.")
    return state.data["tracks"][position - 1]


def apply(state, api, playlist, channel, execute=False, limit=25, retry_position=None):
    if not state.data.get("source_complete"):
        raise StopTransfer("Source export is incomplete.")
    api.verify_playlist(playlist, channel)
    bind(state, playlist, channel)
    existing = api.members(playlist)
    if retry_position is not None:
        track = get_track(state, retry_position)
        if not execute or track["status"] != "inflight" or track.get("video_id") in existing:
            raise StopTransfer("Retry requires --execute and an absent interrupted insert.")
        track["status"] = "approved"
        state.save()
    seen, planned = set(), set()
    additions = 0
    for track in state.data["tracks"]:
        video = track.get("video_id")
        if not video:
            continue
        if video in existing:
            track["status"] = "duplicate" if video in seen else "added"
            seen.add(video)
            state.save()
            continue
        if track["status"] in {"added", "duplicate"}:
            track["status"] = "inflight"
            state.save()
        if track["status"] == "inflight":
            raise StopTransfer(f"Track {track['position']} has an unknown insert outcome. Inspect YouTube, wait, then explicitly use --retry-position {track['position']} if absent.")
        if track["status"] != "approved":
            continue
        if video in planned or additions >= limit:
            continue
        if not execute:
            print(f"Would add #{track['position']}: {track['title']} -> {video}")
            planned.add(video)
        else:
            track["status"] = "inflight"
            state.save()  # Durable write intent before the remote mutation.
            api.insert(playlist, video)
            track["status"] = "added"
            state.save()
            existing.add(video)
            seen.add(video)
        additions += 1
    if execute:
        verified = api.members(playlist)
        missing = [t for t in state.data["tracks"] if t["status"] in {"added", "duplicate"} and t.get("video_id") not in verified]
        for track in missing:
            track["status"] = "inflight"
        state.data["verification"] = {"at": now(), "unique_videos": len(verified),
            "matched_present": len({t["video_id"] for t in state.data["tracks"] if t.get("video_id") in verified})}
        state.save()
        if missing:
            raise StopTransfer("Some insertions could not be verified. Resume to reconcile.")
    return additions


def export_spotify(state, directory, client_id):
    import spotipy
    from spotipy.oauth2 import SpotifyPKCE
    from spotipy.cache_handler import CacheFileHandler
    if state.data["tracks"] and not state.data.get("spotify_export"):
        raise StopTransfer("Source already imported. Use another state directory.")
    if state.data.get("source_complete"):
        raise StopTransfer("Export already complete. Use another state for a new snapshot.")
    auth = SpotifyPKCE(client_id=client_id, redirect_uri="http://127.0.0.1:8888/callback",
        scope="user-library-read user-read-private", requests_timeout=30,
        cache_handler=CacheFileHandler(cache_path=str(Path(directory) / "spotify-token.json")))
    api = spotipy.Spotify(auth_manager=auth, requests_timeout=30, retries=0, status_retries=0)
    account = api.current_user()
    checkpoint = state.data.setdefault("spotify_export", {"account_id": account["id"], "offset": 0})
    if checkpoint["account_id"] != account["id"]:
        raise StopTransfer("Spotify account changed since the previous export.")
    print("Spotify account:", account["id"])
    while True:
        page = api.current_user_saved_tracks(limit=50, offset=checkpoint["offset"])
        if "total" in checkpoint and checkpoint["total"] != page["total"]:
            raise StopTransfer("Liked-song count changed. Start a new state for a consistent snapshot.")
        checkpoint["total"] = page["total"]
        batch = []
        for item in page["items"]:
            track = item.get("track")
            if not track:
                raise StopTransfer("Spotify returned a missing track. Export is incomplete.")
            batch.append({"position": len(state.data["tracks"]) + len(batch) + 1,
                "title": track["name"], "artists": [a["name"] for a in track["artists"]],
                "album": track["album"]["name"], "duration": track["duration_ms"] / 1000,
                "spotify_url": track.get("external_urls", {}).get("spotify", ""),
                "explicit": track.get("explicit"), "status": "pending", "candidates": []})
        state.data["tracks"].extend(batch)
        checkpoint["offset"] += len(batch)
        state.data["source_total"] = page["total"]
        state.save()
        if not page.get("next"):
            if checkpoint["offset"] != page["total"]:
                raise StopTransfer("Captured count differs from Spotify's total.")
            state.data["source_complete"] = True
            state.save()
            return
        if not batch:
            raise StopTransfer("Empty page before source completion.")


def report(state):
    tracks = state.data["tracks"]
    return {"source_total": state.data.get("source_total", len(tracks)), "captured": len(tracks),
            "source_complete": state.data.get("source_complete", False),
            "matched": sum(bool(t.get("video_id")) for t in tracks),
            "statuses": dict(Counter(t["status"] for t in tracks)),
            "destination": state.data.get("destination"), "verification": state.data.get("verification")}


def parser():
    root = argparse.ArgumentParser(description=__doc__)
    root.add_argument("--state", type=Path, default=DEFAULT_STATE, help="Private checkpoint directory outside Git")
    commands = root.add_subparsers(dest="command", required=True)
    imp = commands.add_parser("import", help="Import a local JSON source/transfer ledger")
    imp.add_argument("file", type=Path)
    export = commands.add_parser("export-spotify", help="Explicit read-only Spotify authorization and export")
    export.add_argument("--client-id", required=True)
    auth = commands.add_parser("auth", help="Explicit Google Desktop OAuth authorization")
    auth.add_argument("--client-secrets", type=Path, required=True)
    commands.add_parser("account", help="Show authorized YouTube channel IDs")
    commands.add_parser("status", help="Local status; no network")
    search = commands.add_parser("search", help="Save candidates without changing playlists")
    search.add_argument("--limit", type=int, default=25)
    search.add_argument("--position", type=int, help="Research one unresolved track again")
    search.add_argument("--trusted-channel", action="append", default=[])
    review = commands.add_parser("review", help="Show saved track candidates")
    review.add_argument("position", type=int)
    approve = commands.add_parser("approve", help="Locally approve an inspected candidate")
    approve.add_argument("position", type=int)
    approve.add_argument("video")
    approve.add_argument("--note", required=True)
    strict = commands.add_parser("approve-strict", help="Preview/approve unique strict candidates")
    strict.add_argument("--execute", action="store_true")
    unavailable = commands.add_parser("unavailable", help="Document a manually researched unavailable track")
    unavailable.add_argument("position", type=int)
    unavailable.add_argument("--note", required=True)
    create = commands.add_parser("create-playlist", help="Create a private destination")
    create.add_argument("--channel", required=True)
    create.add_argument("--execute", action="store_true")
    upload = commands.add_parser("apply", help="Reconcile and preview; --execute enables writes")
    upload.add_argument("--playlist", required=True)
    upload.add_argument("--channel", required=True)
    upload.add_argument("--limit", type=int, default=25)
    upload.add_argument("--execute", action="store_true")
    upload.add_argument("--retry-position", type=int)
    return root


def run(args):
    if hasattr(args, "limit") and args.limit < 1:
        raise StopTransfer("--limit must be positive.")
    state = State(args.state)
    command = args.command
    if command == "import":
        import_tracks(state, json.loads(args.file.read_text()))
    elif command == "export-spotify":
        export_spotify(state, args.state, args.client_id)
    elif command == "status":
        pass
    elif command == "review":
        print(json.dumps(get_track(state, args.position), ensure_ascii=False, indent=2))
        return
    elif command in {"approve", "unavailable"}:
        track = get_track(state, args.position)
        if track["status"] in {"added", "duplicate", "inflight"}:
            raise StopTransfer("Track has a completed/interrupted insert. Reconcile before editing.")
        if command == "approve":
            video = youtube_id(args.video)
            if video not in {c["id"] for c in track["candidates"]}:
                raise StopTransfer("Video must be among saved candidates. Run search first.")
            track.update(video_id=video, status="approved", approval=args.note)
        else:
            track.pop("video_id", None)
            track.update(status="unavailable", reason=args.note)
        state.save()
    elif command == "approve-strict":
        for track in state.data["tracks"]:
            choices = [c for c in track["candidates"] if c.get("recommendation") == "strict"]
            if track["status"] == "uncertain" and len(choices) == 1:
                print(f"Strict candidate #{track['position']}: {track['title']} -> {choices[0]['id']}")
                if args.execute:
                    track.update(status="approved", video_id=choices[0]["id"], approval="explicit bulk strict approval")
                    state.save()
    elif command == "create-playlist" and not args.execute:
        print("Would create private Spotify Liked Songs. Use --execute to create it.")
        return
    else:
        api = YouTube(args.state, args.client_secrets if command == "auth" else None)
        if command in {"account", "auth"}:
            for account in api.accounts():
                print(account["id"], account["snippet"]["title"])
            return
        if command == "create-playlist":
            print("https://www.youtube.com/playlist?list=" + create_playlist(state, api, args.channel))
        elif command == "search":
            tracks = [get_track(state, args.position)] if args.position else [
                t for t in state.data["tracks"] if t["status"] == "pending"][:args.limit]
            for track in tracks:
                if track.get("video_id"):
                    raise StopTransfer("Track already has an approved match; refusing to overwrite it.")
                query = " ".join(track["artists"] + [track["title"], track["album"]])
                candidates = [assess(track, c, set(args.trusted_channel)) for c in api.search(query)]
                track.update(candidates=candidates, status="uncertain", search_query=query, searched_at=now())
                state.save()
                print(f"Saved {len(candidates)} candidates for #{track['position']}")
        elif command == "apply":
            print("New additions (planned or executed):", apply(state, api, youtube_id(args.playlist, True),
                args.channel, args.execute, args.limit, args.retry_position))
    print(json.dumps(report(state), indent=2))


def main(argv=None):
    args = parser().parse_args(argv)
    os.umask(0o077)
    try:
        with locked(args.state):
            run(args)
    except (StopTransfer, OSError, ValueError, ImportError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # SDK exception messages may contain private request bodies or credentials.
        code = getattr(getattr(exc, "resp", None), "status", None)
        print(f"Stopped: {type(exc).__name__}" + (f" (HTTP {code})" if code else "") +
              ". Check connection, quota, and authorization. Checkpoint saved; no automatic write retry.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
