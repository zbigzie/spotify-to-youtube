import contextlib
from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import spotify_to_youtube as tool
from test_transfer import FakeHttpError, FakeAPI, VIDEO, source


class LimitsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.directory = Path(self.temp.name)
        self.clock = datetime(2026, 10, 18, 1, 0, tzinfo=timezone.utc)

    def limits(self):
        return tool.RateLimits(self.directory, clock=lambda: self.clock, jitter=lambda: 0)

    def error(self, status=429, reason=None, headers=None):
        body = {"error": {"errors": [{"reason": reason}] if reason else []}}
        return tool.safe_api_error(FakeHttpError(json.dumps(body), headers, status), "search.list")

    def test_repeated_429_persists_exponential_cooldown_and_cap(self):
        for delay in [60, 120, 240, 480, 960, 1920, 3600, 3600]:
            limits = self.limits()
            result = limits.failed("search", self.error())
            self.assertEqual(result["classification"], "rate_limited")
            self.assertEqual(datetime.fromisoformat(result["next_retry_at"]), self.clock + timedelta(seconds=delay))
            with self.assertRaises(tool.StopTransfer):
                self.limits().check("search")
            self.limits().check("videos")
            self.clock += timedelta(seconds=delay)
            self.limits().check("search")

    def test_retry_after_is_minimum_even_above_local_cap(self):
        for headers in [{"retry-after": "7200"}, {"retry-after": "Sun, 18 Oct 2026 03:00:00 GMT"}]:
            result = self.limits().failed("search", self.error(headers=headers))
            self.assertEqual(result["next_retry_at"], "2026-10-18T03:00:00+00:00")

    def test_malformed_retry_after_uses_local_backoff(self):
        for header in ["-12", "1.5", "invalid SECRET", "9" * 200]:
            with tempfile.TemporaryDirectory() as directory:
                limits = tool.RateLimits(directory, clock=lambda: self.clock, jitter=lambda: 0)
                result = limits.failed("search", self.error(headers={"retry-after": header}))
                self.assertEqual(result["next_retry_at"], "2026-10-18T01:01:00+00:00")
                self.assertNotIn("SECRET", json.dumps(result))

    def test_confirmed_quota_uses_next_pacific_midnight(self):
        for clock, expected in [(self.clock, "2026-10-18T07:00:00+00:00"),
                                (datetime(2026, 12, 1, 1, tzinfo=timezone.utc), "2026-12-01T08:00:00+00:00")]:
            self.clock = clock
            result = self.limits().failed("search", self.error(403, "quotaExceeded"))
            self.assertEqual(result["classification"], "quota_exceeded")
            self.assertEqual(result["next_retry_at"], expected)

    def test_forbidden_is_not_inferred_to_be_quota(self):
        result = self.limits().failed("search", self.error(403, "forbidden"))
        self.assertEqual(result["classification"], "authorization_or_permission")
        self.assertIsNone(result["next_retry_at"])

    def test_invalid_checkpoint_fails_closed(self):
        for data in [[], {"schema": 1, "buckets": {"search": {"next_retry_at": "bad"}}}]:
            tool.save_json(self.directory / "api-limits.json", data)
            with self.assertRaises(tool.StopTransfer):
                self.limits()

    def test_server_error_jitter_and_cooldown_file_permissions(self):
        limits = tool.RateLimits(self.directory, clock=lambda: self.clock, jitter=lambda: 0.25)
        result = limits.failed("videos", self.error(503))
        self.assertEqual(result["classification"], "server_error")
        self.assertEqual(result["next_retry_at"], "2026-10-18T01:01:15+00:00")
        for name in ["api-limits.json", "last-api-error.json"]:
            self.assertEqual((self.directory / name).stat().st_mode & 0o777, 0o600)

    def test_successful_other_resource_does_not_clear_search_limit(self):
        limits = self.limits()
        limits.failed("search", self.error())
        limits.succeeded("videos")
        self.assertIn("search", limits.data["buckets"])
        limits.succeeded("search")
        self.assertNotIn("search", self.limits().data["buckets"])

    def test_sdk_called_once_and_cooldown_blocks_next_attempt(self):
        api = object.__new__(tool.YouTube)
        api.directory, api.limits, api.last_request = self.directory, self.limits(), None
        api.api = Mock()
        execute = api.api.search.return_value.list.return_value.execute
        execute.side_effect = FakeHttpError(b'{"error":{"message":"SECRET"}}')
        for _ in range(2):
            with self.assertRaises(tool.StopTransfer) as caught:
                api.call("search", q="synthetic")
            self.assertNotIn("SECRET", str(caught.exception))
        execute.assert_called_once_with(num_retries=0)

    def test_partial_search_cache_survives_details_failure_and_restart(self):
        api = object.__new__(tool.YouTube)
        api.directory = self.directory
        results = {"items": [{"id": {"videoId": VIDEO}}]}
        details = {"items": [{"id": VIDEO, "snippet": {"title": "Example", "channelTitle": "Artist",
                   "channelId": "synthetic", "description": "Album"}, "contentDetails": {"duration": "PT3M"}}]}
        with patch.object(api, "call", side_effect=[results, ConnectionError()]):
            with self.assertRaises(ConnectionError):
                api.search("Example Artist")
        restarted = object.__new__(tool.YouTube)
        restarted.directory = self.directory
        with patch.object(restarted, "call", return_value=details) as call:
            rows = restarted.search("Example Artist")
            self.assertEqual(call.call_args.args[0], "videos")
            call.assert_called_once()
        with patch.object(restarted, "call") as call:
            self.assertEqual(restarted.search("Example Artist"), rows)
            call.assert_not_called()
        with patch.object(restarted, "call", return_value={"items": []}) as call:
            self.assertEqual(restarted.search("Example Artist", refresh=True), [])
            self.assertEqual(call.call_args.args[0], "search")
            restarted.search("Example Artist")
            call.assert_called_once()

    def test_expired_cache_is_refetched(self):
        api = object.__new__(tool.YouTube)
        api.directory = self.directory
        with patch.object(api, "call", return_value={"items": []}), patch.object(tool, "now", return_value="2000-01-01T00:00:00+00:00"):
            api.search("Example")
        with patch.object(api, "call", return_value={"items": []}) as call:
            api.search("Example")
            call.assert_called_once()

    def test_keyboard_interrupt_after_insert_reconciles_without_duplicate(self):
        state = tool.State(self.directory)
        tool.import_tracks(state, [dict(source(), status="matched", youtubeUrl=VIDEO)])
        api = FakeAPI()
        original = api.insert
        def interrupted(*args):
            original(*args)
            raise KeyboardInterrupt()
        with patch.object(api, "insert", side_effect=interrupted):
            with self.assertRaises(KeyboardInterrupt):
                tool.apply(state, api, "PLexample", "channel", execute=True)
        self.assertEqual(tool.State(self.directory).data["tracks"][0]["status"], "inflight")
        tool.apply(tool.State(self.directory), api, "PLexample", "channel", execute=True)
        self.assertEqual(api.inserted, [VIDEO])

    def test_interrupt_cli_is_safe_and_returns_130(self):
        output = io.StringIO()
        with patch.object(tool, "run", side_effect=KeyboardInterrupt), contextlib.redirect_stderr(output):
            self.assertEqual(tool.main(["--state", str(self.directory), "status"]), 130)
        self.assertIn("Checkpoint retained", output.getvalue())


if __name__ == "__main__":
    unittest.main()
