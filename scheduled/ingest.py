#!/usr/bin/env python3
"""
OpenClaw daily use-case ingest
==============================
This script is called by Cowork after Claude has already done the web search
and extraction. It receives records as JSON on stdin (or via --records argument)
and writes them to Firestore.

Usage (called by Cowork scheduled task):
  echo '[{...}, {...}]' | python3 ingest.py

Or for a dry run:
  echo '[{...}]' | DRY_RUN=1 python3 ingest.py

Environment variables (in .env):
  GOOGLE_APPLICATION_CREDENTIALS   path to service_account.json
  FIRESTORE_PROJECT_ID              Firebase project ID
  DRY_RUN                           if "1", print records but don't write
"""

from __future__ import annotations

import os
import sys
import json
import datetime
import logging
from pathlib import Path

import requests
from dotenv import load_dotenv
from google.oauth2 import service_account
import google.auth.transport.requests

load_dotenv(Path(__file__).parent / ".env")

FIRESTORE_PROJECT_ID = os.environ["FIRESTORE_PROJECT_ID"]
DRY_RUN              = os.environ.get("DRY_RUN", "0") == "1"
CREDENTIALS_FILE     = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(Path(__file__).parent / "service_account.json"),
)

COLLECTION     = "use_cases"
FIRESTORE_BASE = (
    f"https://firestore.googleapis.com/v1/projects/{FIRESTORE_PROJECT_ID}"
    f"/databases/(default)/documents"
)
ALLOWED_FIELDS = {
    "category", "sourceUser", "description", "refUrls", "tweetDate",
    "searchDate", "notes", "uncertainty", "novelty", "title",
    "sourceUrl", "subcategory", "confidence", "tags",
}

# Map snake_case variants (from external JSON sources) to canonical camelCase.
FIELD_ALIASES: dict[str, str] = {
    "reference_urls": "refUrls",
    "ref_urls":       "refUrls",
    "source_user":    "sourceUser",
    "source_url":     "sourceUrl",
    "tweet_date":     "tweetDate",
    "search_date":    "searchDate",
}

# Hard cap on records per ingest run — prevents a malicious or oversized
# payload from bulk-writing the collection.
MAX_BATCH = 100

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)


def get_token() -> str:
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/datastore"],
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def safe_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def apply_field_aliases(record: dict) -> dict:
    """Remap any snake_case keys to their canonical camelCase names."""
    return {FIELD_ALIASES.get(k, k): v for k, v in record.items()}


def normalize_record(record: dict):
    cleaned = {}
    for key in ALLOWED_FIELDS:
        if key not in record:
            continue
        value = record.get(key)
        if key in {"refUrls", "sourceUrl"}:
            if not isinstance(value, str):
                return None
            urls = [part.strip() for part in value.split(",") if part.strip()]
            urls = [url for url in urls if safe_url(url)]
            if not urls:
                return None
            cleaned[key] = ", ".join(urls)
        elif key == "tags":
            cleaned[key] = value if isinstance(value, list) else []
        elif key == "confidence":
            if not isinstance(value, (int, float, str)):
                return None
            cleaned[key] = value
        else:
            cleaned[key] = "" if value is None else str(value)
    return cleaned


def url_exists(token, ref_url: str) -> bool:
    resp = requests.post(
        f"{FIRESTORE_BASE}:runQuery",
        headers=headers(token),
        json={"structuredQuery": {
            "from": [{"collectionId": COLLECTION}],
            "where": {"fieldFilter": {
                "field": {"fieldPath": "refUrls"},
                "op": "EQUAL",
                "value": {"stringValue": ref_url},
            }},
            "limit": 1,
        }},
        timeout=30,
    )
    resp.raise_for_status()
    return any("document" in r for r in resp.json())


def write_record(token, record: dict) -> str:
    def fv(v):
        if isinstance(v, str):   return {"stringValue": v}
        if isinstance(v, (int, float)): return {"doubleValue": float(v)}
        if isinstance(v, list):  return {"arrayValue": {"values": [fv(i) for i in v]}}
        return {"nullValue": None}

    now = datetime.datetime.utcnow().isoformat() + "Z"
    fields = {k: fv(v) for k, v in record.items()}
    fields["createdAt"] = {"timestampValue": now}
    fields["updatedAt"] = {"timestampValue": now}

    resp = requests.post(
        f"{FIRESTORE_BASE}/{COLLECTION}",
        headers=headers(token),
        json={"fields": fields},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("name", "").split("/")[-1]


def main():
    raw = sys.stdin.read().strip()
    if not raw:
        log.error("No records received on stdin")
        sys.exit(1)

    try:
        records = json.loads(raw)
    except json.JSONDecodeError as e:
        log.error(f"Invalid JSON: {e}")
        sys.exit(1)

    if not isinstance(records, list):
        log.error("Input must be a JSON array of records")
        sys.exit(1)

    if len(records) > MAX_BATCH:
        log.error(
            f"Batch too large: {len(records)} records exceeds MAX_BATCH={MAX_BATCH}. "
            "Aborting to prevent bulk writes. Split into smaller batches."
        )
        sys.exit(1)

    log.info(f"Received {len(records)} candidate records")

    if DRY_RUN:
        for r in records:
            print(json.dumps(apply_field_aliases(r), indent=2))
        log.info("DRY RUN — nothing written")
        return

    token = get_token()
    inserted = skipped = invalid = 0

    for rec in records:
        # Normalise field names first (handles snake_case input from external sources)
        clean = normalize_record(apply_field_aliases(rec))
        if not clean or not clean.get("description"):
            invalid += 1
            continue

        url = clean.get("refUrls", "").strip()
        if not url:
            # A missing or empty refUrls means dedup cannot work — reject outright
            # rather than letting the record bypass the duplicate check.
            log.warning(f"  SKIP (no refUrls)  {clean.get('description','')[:60]}")
            invalid += 1
            continue

        if url_exists(token, url):
            log.info(f"  DUP  {clean.get('description','')[:60]}")
            skipped += 1
            continue

        doc_id = write_record(token, clean)
        log.info(f"  NEW  {clean.get('description','')[:60]}  → {doc_id}")
        inserted += 1

    log.info(f"Done. inserted={inserted} dupes={skipped} invalid={invalid}")


if __name__ == "__main__":
    main()
