from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SCHEDULED_DIR = Path(__file__).resolve().parents[1] / "scheduled"
MODULE_PATH = SCHEDULED_DIR / "agent_usecases_cli.py"
LEGACY_MODULE_PATH = SCHEDULED_DIR / "openclaw_db_cli.py"

os.environ.setdefault("FIRESTORE_PROJECT_ID", "test-project")
sys.path.insert(0, str(SCHEDULED_DIR))

spec = importlib.util.spec_from_file_location("agent_usecases_cli", MODULE_PATH)
assert spec and spec.loader
agent_usecases_cli = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent_usecases_cli)

legacy_spec = importlib.util.spec_from_file_location("openclaw_db_cli", LEGACY_MODULE_PATH)
assert legacy_spec and legacy_spec.loader
openclaw_db_cli = importlib.util.module_from_spec(legacy_spec)
legacy_spec.loader.exec_module(openclaw_db_cli)


class FakeClient:
    def query_use_cases(self, **kwargs):
        return [{
            "_id": "abc123",
            "seqId": 7,
            "title": "Agent workflows for finance",
            "description": "A good match for financial operations agents.",
            "notes": "Mentioned in production pilot.",
            "category": "finance",
            "novelty": "high",
            "uncertainty": "low",
            "sourceUser": "alice",
            "tweetDate": "2026-06-19",
            "searchDate": "2026-06-20",
            "sourceUrl": "https://example.com/source",
            "refUrls": ["https://example.com/ref"],
            "tags": ["agent", "finance"],
        }]

    def get_record(self, id_or_seq):
        return {"_id": str(id_or_seq), "seqId": 7}

    def get_stats(self):
        return {"total_records": 1}

    def list_categories(self):
        return [{"category": "finance", "count": 1}]

    def get_groups(self, status=None, limit=20):
        return [{"id": "group-1", "status": status or "active"}]

    def get_suggestion_queue(self, limit=20):
        return [{"topic": "agents"}]

    def invalidate_cache(self):
        return {"use_cases": True}


class TestAgentUsecasesCli(unittest.TestCase):
    def test_search_emits_json(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), mock.patch.object(agent_usecases_cli, "FirestoreClient", return_value=FakeClient()):
            rc = agent_usecases_cli.run_cli(["search", "finance agents"])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["count"], 1)
        self.assertEqual(payload["results"][0]["_id"], "abc123")

    def test_last30days_source_maps_items(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), mock.patch.object(agent_usecases_cli, "FirestoreClient", return_value=FakeClient()):
            rc = agent_usecases_cli.run_cli(["last30days-source", "finance agents"])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["count"], 1)
        item = payload["items"][0]
        self.assertEqual(item["source"], "agent_usecases")
        self.assertEqual(item["author"], "alice")
        self.assertEqual(item["container"], "finance")
        self.assertEqual(item["url"], "https://example.com/ref")
        self.assertEqual(item["metadata"]["doc_id"], "abc123")
        self.assertEqual(item["metadata"]["backend"], "openclaw-explorer")

    def test_legacy_shim_exports_run_cli(self):
        stdout = io.StringIO()
        with contextlib.redirect_stdout(stdout), mock.patch.dict(openclaw_db_cli.run_cli.__globals__, {"FirestoreClient": lambda: FakeClient()}):
            rc = openclaw_db_cli.run_cli(["search", "finance agents"])
        self.assertEqual(rc, 0)
        payload = json.loads(stdout.getvalue())
        self.assertEqual(payload["results"][0]["_id"], "abc123")


if __name__ == "__main__":
    unittest.main()
