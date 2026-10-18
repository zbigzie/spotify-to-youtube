"""Local playlist transfer. No browser cookies, hosted services, or Spotify writes."""
from __future__ import annotations

import argparse
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
import hashlib
import html
import json
import os
from pathlib import Path
import re
import random
import sys
import tempfile
import time
import unicodedata
from urllib.parse import parse_qs, urlparse
import uuid
from zoneinfo import ZoneInfo

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
DEFAULT_STATE = Path.home() / ".local/share/spotify-to-youtube"
CACHE_SECONDS = 86400


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


def safe_api_error(exc, command):
    """Keep only bounded, allowlisted diagnostics; never serialize the exception."""
    response = getattr(exc, "resp", None)
    status = getattr(response, "status", None)
    result = {"at": now(), "command": command,
              "http_status": status if type(status) is int and 400 <= status <= 599 else None,
              "reasons": [], "retry_after_seconds": None, "retry_after_utc": None}
    allowed_reasons = {"quotaExceeded", "dailyLimitExceeded", "rateLimitExceeded",
                       "userRateLimitExceeded", "forbidden", "authError",
                       "insufficientPermissions", "accessNotConfigured",
                       "RATE_LIMIT_EXCEEDED", "QUOTA_EXCEEDED", "DAILY_LIMIT_EXCEEDED"}
    content = getattr(exc, "content", None)
    if isinstance(content, (str, bytes)) and len(content) <= 65536:
        try:
            error = json.loads(content).get("error", {})
            if isinstance(error, dict):
                for key in ("errors", "details"):
                    entries = error.get(key, [])
                    if isinstance(entries, list):
                        for entry in entries[:20]:
                            reason = entry.get("reason") if isinstance(entry, dict) else None
                            if isinstance(reason, str) and reason in allowed_reasons:
                                result["reasons"].append(reason)
                if error.get("status") in {"RESOURCE_EXHAUSTED", "PERMISSION_DENIED",
                                           "UNAUTHENTICATED", "UNAVAILABLE"}:
                    result["api_status"] = error["status"]
        except (ValueError, AttributeError, TypeError):
            pass
    result["reasons"] = sorted(set(result["reasons"]))
    if hasattr(response, "get"):
        value = response.get("retry-after") or response.get("Retry-After")
        if isinstance(value, str) and len(value) <= 128:
            value = value.strip()
            if re.fullmatch(r"[0-9]{1,10}", value):
                result["retry_after_seconds"] = int(value)
            else:
                try:
                    date = parsedate_to_datetime(value)
                    if date.tzinfo is not None:
                        result["retry_after_utc"] = date.astimezone(timezone.utc).isoformat()
                except (ValueError, TypeError, OverflowError):
                    pass
    return result


class RateLimits:
    """Persist deferred retries. Each invocation makes at most one failed attempt."""
    def __init__(self, directory, clock=None, jitter=None):
        self.path = Path(directory) / "api-limits.json"
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.jitter = jitter or (lambda: random.uniform(0, 0.25))
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"schema": 1, "buckets": {}}
        if not isinstance(self.data, dict) or self.data.get("schema") != 1 or not isinstance(self.data.get("buckets"), dict):
            raise StopTransfer("Invalid API limit checkpoint; inspect it before resuming.")
        for entry in self.data["buckets"].values():
            try:
                valid = (isinstance(entry, dict) and isinstance(entry.get("classification"), str)
                         and type(entry.get("consecutive_failures")) is int
                         and entry["consecutive_failures"] > 0
                         and datetime.fromisoformat(entry["next_retry_at"]).tzinfo is not None)
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                raise StopTransfer("Invalid API limit checkpoint; inspect it before resuming.")

    @staticmethod
    def bucket(resource):
        return "search" if resource == "search" else "general"

    def check(self, resource):
        bucket = self.bucket(resource)
        entry = self.data["buckets"].get(bucket, {})
        retry = entry.get("next_retry_at")
        if retry and self.clock() < datetime.fromisoformat(retry):
            raise StopTransfer(f"{bucket} requests paused ({entry['classification']}) until {retry}. "
                               "Progress saved; resume the same command after that time.")

    def failed(self, resource, diagnostic):
        bucket = self.bucket(resource)
        current = self.clock()
        reasons = set(diagnostic["reasons"])
        code = diagnostic["http_status"]
        quota = {"quotaExceeded", "dailyLimitExceeded", "QUOTA_EXCEEDED", "DAILY_LIMIT_EXCEEDED"}
        rate = {"rateLimitExceeded", "userRateLimitExceeded", "RATE_LIMIT_EXCEEDED"}
        classification = "other_error"
        retry = None
        count = self.data["buckets"].get(bucket, {}).get("consecutive_failures", 0) + 1
        if code in {403, 429} and reasons & quota:
            classification = "quota_exceeded"
            pacific = current.astimezone(ZoneInfo("America/Los_Angeles"))
            retry = (pacific + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
        elif code == 429 or (code == 403 and reasons & rate) or code in {500, 502, 503, 504}:
            classification = "rate_limited" if code in {403, 429} else "server_error"
            delay = min(3600, 60 * 2 ** min(count - 1, 6) * (1 + self.jitter()))
            retry = current + timedelta(seconds=delay)
        elif code in {401, 403}:
            classification = "authorization_or_permission"
        # Retry-After is a minimum, including for error types without a default delay.
        server_retry = None
        if diagnostic["retry_after_seconds"] is not None:
            server_retry = current + timedelta(seconds=diagnostic["retry_after_seconds"])
        elif diagnostic["retry_after_utc"]:
            server_retry = datetime.fromisoformat(diagnostic["retry_after_utc"])
        if server_retry and server_retry > current:
            retry = max(retry, server_retry) if retry else server_retry
        diagnostic.update(bucket=bucket, classification=classification,
                          consecutive_failures=count,
                          next_retry_at=retry.astimezone(timezone.utc).isoformat() if retry else None)
        if retry:
            self.data["buckets"][bucket] = dict(diagnostic)
            save_json(self.path, self.data)
        save_json(self.path.parent / "last-api-error.json", diagnostic)
        return diagnostic

    def succeeded(self, resource):
        bucket = self.bucket(resource)
        entry = self.data["buckets"].get(bucket, {})
        if entry.get("command", "").split(".")[0] == resource:
            del self.data["buckets"][bucket]
            save_json(self.path, self.data)


def fresh(timestamp):
    try:
        age = (datetime.now(timezone.utc) - datetime.fromisoformat(timestamp)).total_seconds()
        return 0 <= age < CACHE_SECONDS
    except (TypeError, ValueError):
        return False


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
        self.directory = Path(directory)
        self.limits = RateLimits(directory)
        self.last_request = None
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
        token = Path(directory) / "youtube-token.json"
        if client_file:
            from google_auth_oauthlib.flow import InstalledAppFlow
            flow = InstalledAppFlow.from_client_secrets_file(str(client_file), SCOPES)
            credentials = flow.run_local_server(host="127.0.0.1", port=0, timeout_seconds=180,
                authorization_prompt_message="Complete authorization in your browser; keep the URL private.")
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
        self.limits.check(resource)
        if self.last_request is not None:
            time.sleep(max(0, 1 - (time.monotonic() - self.last_request)))
        try:
            result = getattr(getattr(self.api, resource)(), method)(**kwargs).execute(num_retries=0)
        except Exception as exc:
            diagnostic = self.limits.failed(resource, safe_api_error(exc, resource + "." + method))
            retry = diagnostic["next_retry_at"]
            detail = ", ".join(diagnostic["reasons"]) or "server reason unavailable"
            message = f"API {diagnostic['http_status']}: {diagnostic['classification']} ({detail}). "
            message += f"Next retry no earlier than {retry}. " if retry else "Inspect authorization/connection before resuming. "
            raise StopTransfer(message + "Sanitized diagnostics saved; no automatic retry.") from None
        finally:
            self.last_request = time.monotonic()
        self.limits.succeeded(resource)
        return result

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

    def search(self, query, refresh=False):
        cache_path = self.directory / "search-cache.json"
        cache = json.loads(cache_path.read_text()) if cache_path.exists() else {}
        key = hashlib.sha256(("v1:" + query).encode()).hexdigest()
        entry = cache.get(key, {})
        if refresh or not fresh(entry.get("at")):
            entry = {}
        if "candidates" in entry:
            self.last_search_at = entry["at"]
            return entry["candidates"]
        if "results" in entry:
            results = entry["results"]
        else:
            results = self.call("search", part="snippet", type="video", q=query, maxResults=8, safeSearch="none")
            entry = {"at": now(), "results": results}
            cache[key] = entry
            save_json(cache_path, cache)  # Preserve search before a details request can fail.
        ids = [x["id"]["videoId"] for x in results.get("items", [])]
        self.last_search_at = entry["at"]
        if not ids:
            entry["candidates"] = []
            save_json(cache_path, cache)
            return []
        details = self.call("videos", part="snippet,contentDetails", id=",".join(ids))
        rows = []
        for item in details.get("items", []):
            s = item["snippet"]
            rows.append({"id": item["id"], "title": html.unescape(s["title"]), "channel": s["channelTitle"],
                "channel_id": s["channelId"], "description": s.get("description", ""),
                "duration": seconds(item["contentDetails"].get("duration")),
                "url": "https://www.youtube.com/watch?v=" + item["id"]})
        entry["candidates"] = rows
        save_json(cache_path, cache)
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
            "destination": state.data.get("destination"), "verification": state.data.get("verification"),
            "api_limits": RateLimits(state.path.parent).data["buckets"]}


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
    search.add_argument("--refresh", action="store_true", help="Refresh cached search results; cooldowns still apply")
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
                if not args.refresh and fresh(track.get("searched_at")) and track.get("search_query") == query:
                    raw_candidates = track["candidates"]
                    searched_at = track["searched_at"]
                    print(f"Reusing saved candidates for #{track['position']}")
                else:
                    raw_candidates = api.search(query, refresh=args.refresh)
                    searched_at = api.last_search_at
                candidates = [assess(track, c, set(args.trusted_channel)) for c in raw_candidates]
                track.update(candidates=candidates, status="uncertain", search_query=query, searched_at=searched_at)
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
    except KeyboardInterrupt:
        print("Interrupted. Checkpoint retained; resume to reconcile any pending write. No automatic retry.", file=sys.stderr)
        return 130
    except (StopTransfer, OSError, ValueError, ImportError) as exc:
        print(f"Stopped: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # SDK exception messages may contain private request bodies or credentials.
        diagnostic = safe_api_error(exc, args.command)
        code = diagnostic["http_status"]
        try:
            save_json(args.state / "last-api-error.json", diagnostic)
        except OSError:
            print("Could not save sanitized API diagnostics.", file=sys.stderr)
        print(f"Stopped: {type(exc).__name__}" + (f" (HTTP {code})" if code else "") +
              ". Check connection, quota, and authorization. Checkpoint saved; no automatic write retry.", file=sys.stderr)
        print("Sanitized API diagnostics: " + json.dumps(diagnostic), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
