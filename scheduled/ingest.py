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
import time
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
    "sourceUrl", "subcategory", "confidence", "tags", "increaseScope",
}

# Valid increaseScope labels (from tag_increase_scope.py CATEGORY_LABELS)
INCREASE_SCOPE_LABELS = {"News", "Tutorials", "Launches", "Analysis", "Ecosystem", "Duplicates", "Blueprints", "Curated"}

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
REQUEST_TIMEOUT = 60
READ_RETRY_ATTEMPTS = 3
READ_RETRY_DELAY_SECONDS = 2
WRITE_RETRY_DELAY_SECONDS = 2

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
        elif key == "increaseScope":
            if value not in INCREASE_SCOPE_LABELS:
                log.warning(f"  Invalid increaseScope value '{value}' — valid: {sorted(INCREASE_SCOPE_LABELS)}")
                return None
            cleaned[key] = value
        else:
            cleaned[key] = "" if value is None else str(value)
    return cleaned


def is_transient_request_error(exc: requests.exceptions.RequestException) -> bool:
    if isinstance(exc, (requests.exceptions.Timeout, requests.exceptions.ConnectionError)):
        return True
    if isinstance(exc, requests.exceptions.HTTPError) and exc.response is not None:
        return exc.response.status_code in {408, 429, 500, 502, 503, 504}
    return False


def run_read_with_retry(operation_name: str, callback):
    last_exc = None
    for attempt in range(1, READ_RETRY_ATTEMPTS + 1):
        try:
            return callback()
        except requests.exceptions.RequestException as exc:
            last_exc = exc
            if not is_transient_request_error(exc) or attempt == READ_RETRY_ATTEMPTS:
                raise
            log.warning(
                "%s transient failure (%s/%s): %s — retrying in %ss",
                operation_name,
                attempt,
                READ_RETRY_ATTEMPTS,
                exc,
                READ_RETRY_DELAY_SECONDS,
            )
            time.sleep(READ_RETRY_DELAY_SECONDS)

    raise last_exc  # pragma: no cover


def url_exists(token, ref_url: str) -> bool:
    def do_query():
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
            timeout=REQUEST_TIMEOUT,
        )
        resp.raise_for_status()
        return any("document" in r for r in resp.json())

    return run_read_with_retry(f"duplicate check for {ref_url}", do_query)


def write_record(token, record: dict) -> str:
    def fv(v):
        if isinstance(v, str):
            return {"stringValue": v}
        if isinstance(v, (int, float)):
            return {"doubleValue": float(v)}
        if isinstance(v, list):
            return {"arrayValue": {"values": [fv(i) for i in v]}}
        return {"nullValue": None}

    now = datetime.datetime.now(datetime.UTC).isoformat().replace("+00:00", "Z")
    fields = {k: fv(v) for k, v in record.items()}
    fields["createdAt"] = {"timestampValue": now}
    fields["updatedAt"] = {"timestampValue": now}

    resp = requests.post(
        f"{FIRESTORE_BASE}/{COLLECTION}",
        headers=headers(token),
        json={"fields": fields},
        timeout=REQUEST_TIMEOUT,
    )
    resp.raise_for_status()
    return resp.json().get("name", "").split("/")[-1]


def verify_or_retry_ambiguous_write(token, record: dict, ref_url: str, description: str) -> str:
    short_desc = description[:60]
    log.warning(
        "  WRITE TRANSIENT FAILURE  %s  — rechecking Firestore before retry",
        short_desc,
    )

    if url_exists(token, ref_url):
        log.info("  NEW? recovered via existence check  %s", short_desc)
        return ""

    log.warning(
        "  WRITE AMBIGUOUS/ABSENT  %s  — sleeping %ss before one final retry",
        short_desc,
        WRITE_RETRY_DELAY_SECONDS,
    )
    time.sleep(WRITE_RETRY_DELAY_SECONDS)

    try:
        return write_record(token, record)
    except requests.exceptions.RequestException as exc:
        if not is_transient_request_error(exc):
            raise
        if url_exists(token, ref_url):
            log.info("  NEW? recovered after retry via existence check  %s", short_desc)
            return ""
        raise RuntimeError(
            f"Ambiguous Firestore write for {short_desc!r}: transient failures persisted and no duplicate was observable after retry"
        ) from exc


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

        description = clean.get("description", "")
        try:
            doc_id = write_record(token, clean)
            log.info(f"  NEW  {description[:60]}  → {doc_id}")
            inserted += 1
            continue
        except requests.exceptions.RequestException as exc:
            if not is_transient_request_error(exc):
                raise

        doc_id = verify_or_retry_ambiguous_write(token, clean, url, description)
        if doc_id:
            log.info(f"  NEW  {description[:60]}  → {doc_id} (after retry)")
        inserted += 1

    log.info(f"Done. inserted={inserted} dupes={skipped} invalid={invalid}")


if __name__ == "__main__":
    main()
