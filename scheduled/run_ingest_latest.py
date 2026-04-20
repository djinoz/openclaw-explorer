#!/usr/bin/env python3
import os
import pathlib
import subprocess
import sys

base = pathlib.Path(os.environ.get('OPENCLAW_SCHEDULED_DIR', pathlib.Path(__file__).resolve().parent))
files = sorted(base.glob('pending_records_*.json'), key=lambda p: p.stat().st_mtime)
if not files:
    print('ERROR: no pending_records_*.json found', file=sys.stderr)
    sys.exit(1)
latest = files[-1]
stat = subprocess.run(
    ['stat', '-f', 'mtime=%Sm %N', '-t', '%Y-%m-%d %H:%M:%S %z', str(latest)],
    capture_output=True,
    text=True,
    check=True,
)
sys.stdout.write(stat.stdout)
venv_python = base / '.venv' / 'bin' / 'python'
python_bin = str(venv_python if venv_python.exists() else sys.executable)
with latest.open('rb') as f:
    proc = subprocess.run([python_bin, 'ingest.py'], cwd=base, stdin=f, capture_output=True, text=True)
sys.stdout.write(proc.stdout)
sys.stderr.write(proc.stderr)
sys.exit(proc.returncode)
