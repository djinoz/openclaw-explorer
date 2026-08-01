import json
import os
import pathlib
import subprocess
import sys
import tempfile
import textwrap
import unittest


REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scheduled" / "run_ingest_latest.py"


class RunIngestLatestTest(unittest.TestCase):
    def run_wrapper(self, base_dir: pathlib.Path) -> subprocess.CompletedProcess[str]:
        env = os.environ.copy()
        env["OPENCLAW_SCHEDULED_DIR"] = str(base_dir)
        return subprocess.run(
            [sys.executable, str(SCRIPT)],
            capture_output=True,
            text=True,
            env=env,
            cwd=REPO_ROOT,
        )

    def make_runtime(self, base_dir: pathlib.Path, ingest_body: str) -> pathlib.Path:
        ingest_path = base_dir / "ingest.py"
        ingest_path.write_text(textwrap.dedent(ingest_body))
        return ingest_path

    def test_uses_latest_pending_file_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            invocation_log = base / "invocations.jsonl"
            payload_log = base / "payload.txt"
            older = base / "pending_records_2026-06-21_0900.json"
            newer = base / "pending_records_2026-06-21_0915.json"
            older.write_text('[{"record":"old"}]')
            newer.write_text('[{"record":"new"}]')
            os.utime(older, (1000, 1000))
            os.utime(newer, (2000, 2000))

            self.make_runtime(
                base,
                f"""
                import json
                import pathlib
                import sys

                base = pathlib.Path({str(base)!r})
                payload = sys.stdin.read()
                (base / 'payload.txt').write_text(payload)
                with (base / 'invocations.jsonl').open('a') as f:
                    f.write(json.dumps({{'payload': payload}}) + '\\n')
                print('ingest saw payload')
                """,
            )

            proc = self.run_wrapper(base)

            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn(str(newer), proc.stdout)
            self.assertIn('ingest saw payload', proc.stdout)
            self.assertEqual(payload_log.read_text(), newer.read_text())
            lines = invocation_log.read_text().strip().splitlines()
            self.assertEqual(len(lines), 1)
            self.assertEqual(json.loads(lines[0])["payload"], newer.read_text())

    def test_does_not_retry_failed_ingest(self):
        with tempfile.TemporaryDirectory() as tmp:
            base = pathlib.Path(tmp)
            invocation_log = base / "invocations.txt"
            pending = base / "pending_records_2026-06-21_0915.json"
            pending.write_text('[{"record":"only"}]')

            self.make_runtime(
                base,
                f"""
                import pathlib
                import sys

                base = pathlib.Path({str(base)!r})
                payload = sys.stdin.read()
                with (base / 'invocations.txt').open('a') as f:
                    f.write(payload + '\\n')
                print('transient-looking timeout text', file=sys.stderr)
                print('firestore.googleapis.com timed out while checking duplicates', file=sys.stderr)
                sys.exit(17)
                """,
            )

            proc = self.run_wrapper(base)

            self.assertEqual(proc.returncode, 17)
            self.assertIn('firestore.googleapis.com timed out', proc.stderr)
            self.assertEqual(invocation_log.read_text().strip().splitlines(), [pending.read_text()])


if __name__ == '__main__':
    unittest.main()
