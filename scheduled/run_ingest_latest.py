#!/usr/bin/env python3
"""Run ingest.py against the newest pending_records_*.json file.

This wrapper intentionally stays thin: it selects the latest pending file,
emits a timestamped stat line for operator visibility, and invokes ingest.py
exactly once. Canonical Firestore retry/idempotency behavior lives in
scheduled/ingest.py; duplicating retries here would risk double-applying or
conflicting with that contract.
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import sys


def resolve_base() -> pathlib.Path:
    return pathlib.Path(
        os.environ.get("OPENCLAW_SCHEDULED_DIR", pathlib.Path(__file__).resolve().parent)
    )


def latest_pending_file(base: pathlib.Path) -> pathlib.Path:
    files = sorted(base.glob("pending_records_*.json"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise FileNotFoundError("no pending_records_*.json found")
    return files[-1]


def print_stat(latest: pathlib.Path) -> None:
    stat = subprocess.run(
        ["stat", "-f", "mtime=%Sm %N", "-t", "%Y-%m-%d %H:%M:%S %z", str(latest)],
        capture_output=True,
        text=True,
        check=True,
    )
    sys.stdout.write(stat.stdout)


def resolve_python(base: pathlib.Path) -> str:
    venv_python = base / ".venv" / "bin" / "python"
    return str(venv_python if venv_python.exists() else sys.executable)


def run_once(base: pathlib.Path, latest: pathlib.Path, python_bin: str) -> subprocess.CompletedProcess[str]:
    with latest.open("rb") as f:
        return subprocess.run(
            [python_bin, "ingest.py"],
            cwd=base,
            stdin=f,
            capture_output=True,
            text=True,
        )


def main() -> int:
    base = resolve_base()
    try:
        latest = latest_pending_file(base)
    except FileNotFoundError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    print_stat(latest)
    proc = run_once(base, latest, resolve_python(base))
    sys.stdout.write(proc.stdout)
    sys.stderr.write(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    sys.exit(main())
