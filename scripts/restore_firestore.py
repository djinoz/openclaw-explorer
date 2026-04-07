#!/usr/bin/env python3
"""
Restore use_cases collection after bad clawskills ingest.

The bad records are the 38 from openclaw-usecases-clawskills.json that were
ingested with snake_case field names (reference_urls), causing refUrls to be
empty and all dedup checks to be bypassed.  We identify them precisely by
matching description text against the clawskills JSON — so we do NOT touch
the legitimate older records that happen to also lack refUrls.

Usage:
  # Preview what would be deleted (safe, no writes):
  DRY_RUN=1 python3 scripts/restore_firestore.py

  # Delete bad records:
  python3 scripts/restore_firestore.py

Environment (loaded from scheduled/.env):
  GOOGLE_APPLICATION_CREDENTIALS  path to service_account.json
  FIRESTORE_PROJECT_ID             Firebase project ID
  DRY_RUN                          "1" = preview only
"""

from __future__ import annotations

import os
import sys
import json
import logging
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.oauth2 import service_account
import google.auth.transport.requests

# Load env from scheduled/.env (one level up from scripts/)
load_dotenv(Path(__file__).parent.parent / "scheduled" / ".env")

FIRESTORE_PROJECT_ID = os.environ["FIRESTORE_PROJECT_ID"]
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
_PROJECT_ROOT  = Path(__file__).parent.parent
_SCHEDULED_DIR = _PROJECT_ROOT / "scheduled"

# Descriptions from the bad ingest files — used for precise targeting
CLAWSKILLS_JSONS = [
    _SCHEDULED_DIR / "openclaw-usecases-clawskills.json",
    _SCHEDULED_DIR / "openclaw-usecases-clawskills-v2.json",
]
_raw_creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "./service_account.json")
# Resolve relative paths against the scheduled/ directory (where .env lives)
CREDENTIALS_FILE = str(
    (_SCHEDULED_DIR / _raw_creds).resolve()
    if not Path(_raw_creds).is_absolute()
    else Path(_raw_creds)
)

COLLECTION = "use_cases"
FIRESTORE_BASE = (
    f"https://firestore.googleapis.com/v1/projects/{FIRESTORE_PROJECT_ID}"
    f"/databases/(default)/documents"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def get_token() -> str:
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/datastore"],
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def list_all_documents(token: str) -> list[dict]:
    """Fetch every document from the use_cases collection (handles pagination)."""
    docs: list[dict] = []
    url = f"{FIRESTORE_BASE}/{COLLECTION}"
    params: dict = {"pageSize": 300}
    while True:
        resp = requests.get(url, headers=auth_headers(token), params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        docs.extend(data.get("documents", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        params["pageToken"] = page_token
    return docs


def load_clawskills_descriptions() -> set[str]:
    """Load the exact descriptions from the bad ingest files for precise matching."""
    descs: set[str] = set()
    for path in CLAWSKILLS_JSONS:
        if not path.exists():
            log.warning(f"  JSON not found, skipping: {path}")
            continue
        with open(path, encoding="utf-8") as f:
            records = json.load(f)
        batch = {r["description"].strip() for r in records if r.get("description")}
        log.info(f"  Loaded {len(batch)} descriptions from {path.name}")
        descs |= batch
    return descs


def is_bad_record(doc: dict, bad_descriptions: set[str]) -> bool:
    """
    A bad record is one whose description exactly matches a clawskills.json
    entry AND has no refUrls.  This avoids touching legitimate older records
    that also happen to lack refUrls.
    """
    fields = doc.get("fields", {})
    has_url = "refUrls" in fields or "sourceUrl" in fields
    if has_url:
        return False
    desc = fields.get("description", {}).get("stringValue", "").strip()
    return desc in bad_descriptions


def field_str(doc: dict, field: str, fallback: str = "?") -> str:
    return doc.get("fields", {}).get(field, {}).get("stringValue", fallback)


def delete_document(token: str, doc_name: str) -> None:
    resp = requests.delete(
        f"https://firestore.googleapis.com/v1/{doc_name}",
        headers=auth_headers(token),
        timeout=30,
    )
    resp.raise_for_status()


def main() -> None:
    if DRY_RUN:
        log.info("=== DRY RUN — no documents will be deleted ===")

    bad_descriptions = load_clawskills_descriptions()
    log.info(f"Loaded {len(bad_descriptions)} clawskills descriptions to match against")

    token = get_token()
    log.info("Fetching all use_cases documents…")
    docs = list_all_documents(token)
    log.info(f"Total documents found: {len(docs)}")

    bad = [d for d in docs if is_bad_record(d, bad_descriptions)]
    good = [d for d in docs if not is_bad_record(d, bad_descriptions)]

    log.info(f"  Good records (have refUrls/sourceUrl): {len(good)}")
    log.info(f"  Bad records  (missing refUrls):        {len(bad)}")

    if not bad:
        log.info("No bad records found — collection looks clean.")
        return

    log.info("\nBad records that will be deleted:")
    for doc in bad:
        doc_id = doc["name"].split("/")[-1]
        cat = field_str(doc, "category")
        desc = field_str(doc, "description")[:80]
        log.info(f"  {doc_id}  [{cat}]  {desc}")

    if DRY_RUN:
        log.info(f"\nDRY RUN — would delete {len(bad)} records. Re-run without DRY_RUN=1 to apply.")
        return

    log.info(f"\nDeleting {len(bad)} bad records…")
    deleted = 0
    failed = 0
    for doc in bad:
        doc_id = doc["name"].split("/")[-1]
        try:
            delete_document(token, doc["name"])
            log.info(f"  DELETED  {doc_id}")
            deleted += 1
        except Exception as exc:
            log.error(f"  FAILED   {doc_id}: {exc}")
            failed += 1

    log.info(f"\nDone. deleted={deleted} failed={failed} good_remaining={len(good)}")
    if failed:
        log.warning("Some deletes failed — re-run to retry.")
        sys.exit(1)


if __name__ == "__main__":
    main()
