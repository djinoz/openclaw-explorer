from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

import requests

SCHEDULED_DIR = Path(__file__).resolve().parents[1] / "scheduled"
MODULE_PATH = SCHEDULED_DIR / "ingest.py"

os.environ.setdefault("FIRESTORE_PROJECT_ID", "test-project")
sys.path.insert(0, str(SCHEDULED_DIR))

spec = importlib.util.spec_from_file_location("scheduled_ingest", MODULE_PATH)
assert spec and spec.loader
scheduled_ingest = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduled_ingest)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            response = requests.Response()
            response.status_code = self.status_code
            raise requests.exceptions.HTTPError(response=response)


def make_http_error(status_code: int) -> requests.exceptions.HTTPError:
    response = requests.Response()
    response.status_code = status_code
    return requests.exceptions.HTTPError(response=response)


class ScheduledIngestRetryTests(unittest.TestCase):
    def setUp(self):
        self.record = {
            "description": "demo record",
            "refUrls": "https://example.com/demo",
            "category": "Research",
        }

    def test_url_exists_retries_transient_http_error(self):
        with mock.patch.object(
            scheduled_ingest.requests,
            "post",
            side_effect=[make_http_error(503), FakeResponse([{"document": {"name": "x"}}])],
        ) as post_mock, mock.patch.object(scheduled_ingest.time, "sleep") as sleep_mock:
            exists = scheduled_ingest.url_exists("token", "https://example.com/demo")

        self.assertTrue(exists)
        self.assertEqual(post_mock.call_count, 2)
        sleep_mock.assert_called_once_with(scheduled_ingest.READ_RETRY_DELAY_SECONDS)

    def test_main_recovers_from_transient_write_by_retrying_once(self):
        stdin = io.StringIO('[{"description": "demo record", "refUrls": "https://example.com/demo", "category": "Research"}]')
        with mock.patch.object(scheduled_ingest, "get_token", return_value="token"),              mock.patch.object(scheduled_ingest, "url_exists", side_effect=[False, False]) as exists_mock,              mock.patch.object(scheduled_ingest, "write_record", side_effect=[requests.exceptions.ConnectionError("dropped"), "doc-2"]) as write_mock,              mock.patch.object(scheduled_ingest.time, "sleep") as sleep_mock,              mock.patch.object(sys, "stdin", stdin):
            scheduled_ingest.main()

        self.assertEqual(exists_mock.call_count, 2)
        self.assertEqual(write_mock.call_count, 2)
        sleep_mock.assert_called_once_with(scheduled_ingest.WRITE_RETRY_DELAY_SECONDS)

    def test_main_counts_ambiguous_transient_write_as_inserted_when_duplicate_appears(self):
        stdin = io.StringIO('[{"description": "demo record", "refUrls": "https://example.com/demo", "category": "Research"}]')
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout),              mock.patch.object(scheduled_ingest, "get_token", return_value="token"),              mock.patch.object(scheduled_ingest, "url_exists", side_effect=[False, True]) as exists_mock,              mock.patch.object(scheduled_ingest, "write_record", side_effect=requests.exceptions.Timeout("timed out")) as write_mock,              mock.patch.object(sys, "stdin", stdin):
            scheduled_ingest.main()

        self.assertEqual(exists_mock.call_count, 2)
        self.assertEqual(write_mock.call_count, 1)
        self.assertEqual(stdout.getvalue(), "")

    def test_verify_or_retry_ambiguous_write_raises_when_duplicate_never_appears(self):
        with mock.patch.object(scheduled_ingest, "url_exists", side_effect=[False, False]),              mock.patch.object(scheduled_ingest, "write_record", side_effect=requests.exceptions.Timeout("timed out")),              mock.patch.object(scheduled_ingest.time, "sleep"):
            with self.assertRaisesRegex(RuntimeError, "Ambiguous Firestore write"):
                scheduled_ingest.verify_or_retry_ambiguous_write(
                    "token",
                    self.record,
                    "https://example.com/demo",
                    "demo record",
                )


if __name__ == "__main__":
    unittest.main()
