import copy
import contextlib
import io
import json
import tempfile
import types
import sys
import unittest
from unittest.mock import patch
from pathlib import Path

import spotify_to_youtube as tool

VIDEO, OTHER = "abcdefghijk", "lmnopqrstuv"


class FakeHttpError(Exception):
    def __init__(self, content, headers=None, status=429):
        super().__init__("SECRET exception message and request URL")
        self.content = content
        self.resp = type("Response", (dict,), {})(headers or {})
        self.resp.status = status


class ErrorLoggingTests(unittest.TestCase):
    def test_only_allowlisted_fields_survive(self):
        error = FakeHttpError(json.dumps({"error": {
            "message": "SECRET body", "status": "RESOURCE_EXHAUSTED",
            "errors": [{"reason": "rateLimitExceeded", "message": "SECRET"},
                       {"reason": "SECRET arbitrary reason"}],
            "details": [{"reason": "RATE_LIMIT_EXCEEDED", "metadata": {"token": "SECRET"}}]}}),
            {"retry-after": "120", "Authorization": "Bearer SECRET", "Set-Cookie": "SECRET"})
        result = tool.safe_api_error(error, "search")
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["reasons"], ["RATE_LIMIT_EXCEEDED", "rateLimitExceeded"])
        self.assertEqual(result["retry_after_seconds"], 120)
        self.assertEqual(result["api_status"], "RESOURCE_EXHAUSTED")
        self.assertNotIn("SECRET", json.dumps(result))

    def test_http_date_retry_after_is_canonical(self):
        error = FakeHttpError(b'{}', {"Retry-After": "Sun, 18 Oct 2026 02:00:00 GMT"})
        result = tool.safe_api_error(error, "search")
        self.assertEqual(result["retry_after_utc"], "2026-10-18T02:00:00+00:00")
        self.assertIsNone(result["retry_after_seconds"])

    def test_429_alone_does_not_imply_daily_quota(self):
        result = tool.safe_api_error(FakeHttpError(b'{}'), "search")
        self.assertEqual(result["http_status"], 429)
        self.assertEqual(result["reasons"], [])
        self.assertIsNone(result["retry_after_seconds"])
        self.assertIsNone(result["retry_after_utc"])

    def test_malformed_or_oversized_content_and_headers_are_not_logged(self):
        for content in [b'not JSON SECRET', b'[]', b'{"error":null}',
                        b'{"error":{"errors":"SECRET","status":[]}}', b'SECRET' * 12000]:
            result = tool.safe_api_error(FakeHttpError(content,
                {"retry-after": "SECRET\nAuthorization: token"}, status="SECRET"), "search")
            self.assertNotIn("SECRET", json.dumps(result))
            self.assertIsNone(result["http_status"])
            self.assertIsNone(result["retry_after_seconds"])
            self.assertIsNone(result["retry_after_utc"])

    def test_cli_saves_private_diagnostics_without_retry_or_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            error = FakeHttpError(b'{"error":{"errors":[{"reason":"quotaExceeded"}]}}',
                                  {"retry-after": "900"}, status=403)
            stderr = io.StringIO()
            with patch.object(tool, "run", side_effect=error) as run, contextlib.redirect_stderr(stderr):
                code = tool.main(["--state", directory, "search", "--limit", "1"])
            self.assertEqual(code, 1)
            run.assert_called_once()
            path = Path(directory) / "last-api-error.json"
            result = json.loads(path.read_text())
            self.assertEqual(result["reasons"], ["quotaExceeded"])
            self.assertEqual(result["retry_after_seconds"], 900)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertNotIn("SECRET", path.read_text() + stderr.getvalue())
            self.assertFalse((Path(directory) / "state.json").exists())


def source():
    return {"title": "Example Song - Live", "artists": ["Example Artist"],
            "album": "Live Album", "duration": "3:42"}


def candidate(title="Example Artist - Example Song - Live (Official Audio)"):
    return {"id": VIDEO, "title": title, "description": "Example Artist\nLive Album",
            "channel": "Example Artist", "channel_id": "trusted", "duration": 222}


class FakeAPI:
    def __init__(self, members=()):
        self.existing = set(members)
        self.inserted = []
        self.failure = None
        self.private = True
        self.playlists = []
        self.creates = 0

    def verify_account(self, channel):
        if channel != "channel":
            raise tool.StopTransfer("Wrong account")

    def verify_playlist(self, playlist, channel):
        self.verify_account(channel)
        if not self.private:
            raise tool.StopTransfer("Not private")

    def members(self, playlist):
        return set(self.existing)

    def insert(self, playlist, video):
        self.inserted.append(video)
        if self.failure == "before":
            raise ConnectionError("Disconnected")
        self.existing.add(video)
        if self.failure == "after":
            raise ConnectionError("Response lost")

    def pages(self, resource, **kwargs):
        return iter(self.playlists)

    def call(self, resource, method, **kwargs):
        self.creates += 1
        playlist = copy.deepcopy(kwargs["body"])
        playlist["id"] = "PLexample"
        self.playlists.append(playlist)
        if self.failure == "after":
            raise ConnectionError("Creation response lost")
        return playlist


class TransferTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = tool.State(self.temp.name)

    def imported(self, videos=(VIDEO,)):
        tool.import_tracks(self.state, [dict(source(), status="matched",
            youtubeUrl="https://www.youtube.com/watch?v=" + v) for v in videos])

    def test_imported_completed_insert_is_not_silently_repeated(self):
        tool.import_tracks(self.state, [dict(source(), status="added",
            youtubeUrl="https://www.youtube.com/watch?v=" + VIDEO)])
        api = FakeAPI()
        with self.assertRaises(tool.StopTransfer):
            tool.apply(self.state, api, "PLexample", "channel", execute=True)
        self.assertEqual(api.inserted, [])
        self.assertEqual(self.state.data["tracks"][0]["status"], "inflight")
        api.existing.add(VIDEO)
        tool.apply(tool.State(self.temp.name), api, "PLexample", "channel", execute=True)
        self.assertEqual(api.inserted, [])

    def test_import_retains_clean_and_explicit_source_flags(self):
        tool.import_tracks(self.state, [dict(source(), explicit=False),
            dict(source(), explicit=True), dict(source(), raw="Song\nE\nArtist"),
            dict(source(), raw="Song\nArtist"), source()])
        tracks = self.state.data["tracks"]
        self.assertEqual([t["explicit"] for t in tracks[:4]], [False, True, True, False])
        self.assertNotIn("explicit", tracks[4])

    def test_unapproved_video_id_cannot_trigger_an_insert(self):
        self.imported()
        self.state.data["tracks"][0]["status"] = "uncertain"
        api = FakeAPI()
        self.assertEqual(tool.apply(self.state, api, "PLexample", "channel", execute=True), 0)
        self.assertEqual(api.inserted, [])

    def test_import_preserves_version_and_requires_reconciliation(self):
        self.imported()
        track = self.state.data["tracks"][0]
        self.assertEqual(track["title"], "Example Song - Live")
        self.assertEqual(track["status"], "approved")
        self.assertEqual(track["duration"], 222)
        self.assertEqual(self.state.path.stat().st_mode & 0o777, 0o600)
        with self.assertRaises(tool.StopTransfer):
            tool.import_tracks(self.state, [source()])

    def test_invalid_import_is_atomic(self):
        with self.assertRaises(tool.StopTransfer):
            tool.import_tracks(self.state, [source(), {"title": "Missing artist"}])
        self.assertFalse(self.state.data["tracks"])
        self.assertFalse(self.state.path.exists())

    def test_id_validation(self):
        self.assertEqual(tool.youtube_id("https://www.youtube.com/playlist?list=PLshort", True), "PLshort")
        self.assertEqual(tool.youtube_id("https://youtu.be/" + VIDEO), VIDEO)
        with self.assertRaises(tool.StopTransfer):
            tool.youtube_id("https://example.com/watch?v=" + VIDEO)

    def test_strict_matching_preserves_versions(self):
        track = dict(source(), duration=222)
        self.assertEqual(tool.assess(track, candidate(), {"trusted"})["recommendation"], "strict")
        for title in ["Example Artist - Example Song", "Example Artist - Example Song - Remix",
                      "Example Artist - Example Song - Live (Clean)"]:
            self.assertEqual(tool.assess(track, candidate(title), {"trusted"})["recommendation"], "review")
        self.assertEqual(tool.assess(track, candidate(), set())["recommendation"], "review")
        self.assertEqual(tool.assess(track, dict(candidate(), duration=225), {"trusted"})["recommendation"], "review")

    def test_duplicates_and_repeat_apply(self):
        self.imported((VIDEO, VIDEO, OTHER))
        api = FakeAPI()
        self.assertEqual(tool.apply(self.state, api, "PLexample", "channel", execute=True), 2)
        self.assertEqual(api.inserted, [VIDEO, OTHER])
        self.assertEqual([t["status"] for t in self.state.data["tracks"]], ["added", "duplicate", "added"])
        self.assertEqual(tool.apply(tool.State(self.temp.name), api, "PLexample", "channel", execute=True), 0)
        self.assertEqual(len(api.inserted), 2)

    def test_dry_run_never_claims_planned_duplicates_added(self):
        self.imported((VIDEO, VIDEO))
        api = FakeAPI()
        self.assertEqual(tool.apply(self.state, api, "PLexample", "channel"), 1)
        self.assertEqual(api.inserted, [])
        self.assertEqual([t["status"] for t in self.state.data["tracks"]], ["approved", "approved"])

    def test_dry_run_reconciles_actual_existing_video(self):
        self.imported()
        self.assertEqual(tool.apply(self.state, FakeAPI([VIDEO]), "PLexample", "channel"), 0)
        self.assertEqual(self.state.data["tracks"][0]["status"], "added")

    def test_lost_response_recovers_without_duplicate(self):
        self.imported()
        api = FakeAPI()
        api.failure = "after"
        with self.assertRaises(ConnectionError):
            tool.apply(self.state, api, "PLexample", "channel", execute=True)
        reopened = tool.State(self.temp.name)
        self.assertEqual(reopened.data["tracks"][0]["status"], "inflight")
        api.failure = None
        tool.apply(reopened, api, "PLexample", "channel", execute=True)
        self.assertEqual(api.inserted, [VIDEO])

    def test_absent_interrupted_write_needs_explicit_retry(self):
        self.imported()
        api = FakeAPI()
        api.failure = "before"
        with self.assertRaises(ConnectionError):
            tool.apply(self.state, api, "PLexample", "channel", execute=True)
        api.failure = None
        with self.assertRaises(tool.StopTransfer):
            tool.apply(self.state, api, "PLexample", "channel", execute=True)
        self.assertEqual(len(api.inserted), 1)
        tool.apply(self.state, api, "PLexample", "channel", execute=True, retry_position=1)
        self.assertEqual(len(api.inserted), 2)

    def test_limit_keeps_remaining_approvals(self):
        self.imported((VIDEO, OTHER))
        api = FakeAPI()
        tool.apply(self.state, api, "PLexample", "channel", execute=True, limit=1)
        self.assertEqual(api.inserted, [VIDEO])
        self.assertEqual(self.state.data["tracks"][1]["status"], "approved")

    def test_wrong_account_privacy_and_destination_block_writes(self):
        self.imported()
        api = FakeAPI()
        with self.assertRaises(tool.StopTransfer):
            tool.apply(self.state, api, "PLexample", "wrong", execute=True)
        api.private = False
        with self.assertRaises(tool.StopTransfer):
            tool.apply(self.state, api, "PLexample", "channel", execute=True)
        api.private = True
        tool.bind(self.state, "PLoriginal", "channel")
        with self.assertRaises(tool.StopTransfer):
            tool.apply(self.state, api, "PLexample", "channel", execute=True)
        self.assertEqual(api.inserted, [])

    def test_unapproved_and_incomplete_source_block_writes(self):
        tool.import_tracks(self.state, [source()])
        api = FakeAPI()
        tool.apply(self.state, api, "PLexample", "channel", execute=True)
        self.assertEqual(api.inserted, [])
        self.state.data["source_complete"] = False
        with self.assertRaises(tool.StopTransfer):
            tool.apply(self.state, api, "PLexample", "channel", execute=True)

    def test_creation_recovers_existing_playlist_after_lost_response(self):
        api = FakeAPI()
        api.failure = "after"
        with self.assertRaises(ConnectionError):
            tool.create_playlist(self.state, api, "channel")
        api.failure = None
        self.assertEqual(tool.create_playlist(tool.State(self.temp.name), api, "channel"), "PLexample")
        self.assertEqual(api.creates, 1)
        self.assertEqual(api.playlists[0]["status"]["privacyStatus"], "private")

    def test_unknown_creation_outcome_does_not_retry(self):
        self.state.data["create_attempt"] = {"marker": "example", "channel": "channel", "sent": True}
        api = FakeAPI()
        with self.assertRaises(tool.StopTransfer):
            tool.create_playlist(self.state, api, "channel")
        self.assertEqual(api.creates, 0)

    def test_pagination_follows_empty_page_with_token(self):
        api = object.__new__(tool.YouTube)
        responses = [{"items": [{"id": "one"}], "nextPageToken": "second"},
                     {"items": [], "nextPageToken": "third"}, {"items": [{"id": "three"}]}]
        with patch.object(api, "call", side_effect=responses) as call:
            self.assertEqual(list(api.pages("playlists", part="snippet")), [{"id": "one"}, {"id": "three"}])
            self.assertEqual(call.call_args_list[2].kwargs["pageToken"], "third")

    def test_failed_verification_is_unresolved(self):
        self.imported()
        api = FakeAPI()
        with patch.object(api, "members", side_effect=[set(), set()]):
            with self.assertRaises(tool.StopTransfer):
                tool.apply(self.state, api, "PLexample", "channel", execute=True)
        self.assertEqual(self.state.data["tracks"][0]["status"], "inflight")

    def test_previously_added_but_now_absent_is_not_reinserted(self):
        self.imported()
        self.state.data["tracks"][0]["status"] = "added"
        api = FakeAPI()
        with self.assertRaises(tool.StopTransfer):
            tool.apply(self.state, api, "PLexample", "channel", execute=True)
        self.assertEqual(api.inserted, [])

    def test_status_needs_no_sdk(self):
        self.imported()
        self.assertEqual(tool.main(["--state", self.temp.name, "status"]), 0)

    def spotify_modules(self, api):
        return {"spotipy": types.SimpleNamespace(Spotify=lambda **kw: api),
                "spotipy.oauth2": types.SimpleNamespace(SpotifyPKCE=lambda **kw: None),
                "spotipy.cache_handler": types.SimpleNamespace(CacheFileHandler=lambda **kw: None)}

    def test_spotify_export_resumes_after_page_failure(self):
        raw = {"name": "Example", "artists": [{"name": "Artist"}], "album": {"name": "Album"}, "duration_ms": 100000}
        class Spotify:
            fail = True
            def current_user(self):
                return {"id": "synthetic-user"}
            def current_user_saved_tracks(self, limit, offset):
                if offset == 1 and self.fail:
                    raise ConnectionError("Disconnected")
                return {"items": [{"track": raw}], "total": 2, "next": "next" if offset == 0 else None}
        api = Spotify()
        with patch.dict(sys.modules, self.spotify_modules(api)):
            with self.assertRaises(ConnectionError):
                tool.export_spotify(self.state, self.temp.name, "test-client")
            reopened = tool.State(self.temp.name)
            self.assertEqual(len(reopened.data["tracks"]), 1)
            self.assertFalse(reopened.data.get("source_complete", False))
            api.fail = False
            tool.export_spotify(reopened, self.temp.name, "test-client")
            self.assertTrue(reopened.data["source_complete"])
            self.assertEqual([t["position"] for t in reopened.data["tracks"]], [1, 2])

    def test_spotify_count_change_stops_before_partial_page_append(self):
        class Spotify:
            def current_user(self):
                return {"id": "synthetic-user"}
            def current_user_saved_tracks(self, **kwargs):
                return {"items": [], "total": 3, "next": None}
        self.state.data["spotify_export"] = {"account_id": "synthetic-user", "offset": 0, "total": 2}
        with patch.dict(sys.modules, self.spotify_modules(Spotify())):
            with self.assertRaises(tool.StopTransfer):
                tool.export_spotify(self.state, self.temp.name, "test-client")
        self.assertFalse(self.state.data["tracks"])


if __name__ == "__main__":
    unittest.main()
