#!/usr/bin/env python3
"""
OpenClaw daily use-case ingest
==============================
Discovers new Claude AI use cases via web search, extracts structured records
using the Claude API, deduplicates against Firestore, and writes new records.

Environment variables (set in .env or shell):
  GOOGLE_APPLICATION_CREDENTIALS   path to Firebase service account JSON
  FIRESTORE_PROJECT_ID              Firebase project ID
  ANTHROPIC_API_KEY                 Anthropic API key
  SEARCH_API_KEY                    Tavily API key (https://tavily.com)
  MAX_RECORDS                       max new records to add per run (default 20)
  DRY_RUN                           if "1", print records but don't write to Firestore
"""

import os
import json
import re
import datetime
import logging
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv
import anthropic
import google.auth
import google.auth.transport.requests
from google.oauth2 import service_account

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

load_dotenv(Path(__file__).parent / ".env")

FIRESTORE_PROJECT_ID = os.environ["FIRESTORE_PROJECT_ID"]
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
SEARCH_API_KEY       = os.environ["SEARCH_API_KEY"]
MAX_RECORDS          = int(os.environ.get("MAX_RECORDS", "20"))
DRY_RUN              = os.environ.get("DRY_RUN", "0") == "1"
CREDENTIALS_FILE     = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(Path(__file__).parent / "service_account.json"),
)

COLLECTION = "use_cases"
FIRESTORE_BASE = f"https://firestore.googleapis.com/v1/projects/{FIRESTORE_PROJECT_ID}/databases/(default)/documents"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(Path(__file__).parent / "logs" / "ingest.log"),
    ],
)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Auth helpers
# ---------------------------------------------------------------------------

def get_access_token() -> str:
    """Return a short-lived OAuth2 access token for the service account."""
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/datastore"],
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def firestore_headers(token: str) -> dict:
    return {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }

# ---------------------------------------------------------------------------
# Firestore REST helpers
# ---------------------------------------------------------------------------

def firestore_query(token: str, field: str, op: str, value: str) -> list:
    """Run a simple equality / inequality structured query."""
    url = f"{FIRESTORE_BASE}:runQuery"
    body = {
        "structuredQuery": {
            "from": [{"collectionId": COLLECTION}],
            "where": {
                "fieldFilter": {
                    "field": {"fieldPath": field},
                    "op": op,
                    "value": {"stringValue": value},
                }
            },
            "limit": 1,
        }
    }
    resp = requests.post(url, headers=firestore_headers(token), json=body, timeout=30)
    resp.raise_for_status()
    results = resp.json()
    # Returns list of {document: {...}} or [{}] if empty
    return [r["document"] for r in results if "document" in r]


def url_exists(token: str, source_url: str) -> bool:
    docs = firestore_query(token, "sourceUrl", "EQUAL", source_url)
    return len(docs) > 0


def write_record(token: str, record: dict) -> str:
    """Write a single record to Firestore. Returns the document name."""
    url = f"{FIRESTORE_BASE}/{COLLECTION}"

    def fv(v):
        """Wrap a Python value into a Firestore Value dict."""
        if isinstance(v, str):
            return {"stringValue": v}
        if isinstance(v, (int, float)):
            return {"doubleValue": float(v)}
        if isinstance(v, list):
            return {"arrayValue": {"values": [fv(i) for i in v]}}
        if v is None:
            return {"nullValue": None}
        return {"stringValue": str(v)}

    now = datetime.datetime.utcnow().isoformat() + "Z"
    fields = {k: fv(v) for k, v in record.items()}
    fields["createdAt"] = {"timestampValue": now}
    fields["updatedAt"] = {"timestampValue": now}
    fields.setdefault("tags", {"arrayValue": {"values": []}})

    body = {"fields": fields}
    resp = requests.post(url, headers=firestore_headers(token), json=body, timeout=30)
    resp.raise_for_status()
    return resp.json().get("name", "")

# ---------------------------------------------------------------------------
# Web search (Tavily)
# ---------------------------------------------------------------------------

SEARCH_QUERIES = [
    "Claude AI real world use case 2025",
    "Anthropic Claude enterprise deployment example",
    "Claude AI automation workflow example site:twitter.com OR site:linkedin.com",
    "\"using Claude\" OR \"built with Claude\" AI use case",
]


def search_web(query: str, max_results: int = 10) -> list[dict]:
    """Search via Tavily and return list of {title, url, content} dicts."""
    resp = requests.post(
        "https://api.tavily.com/search",
        json={
            "api_key": SEARCH_API_KEY,
            "query": query,
            "search_depth": "basic",
            "max_results": max_results,
            "include_raw_content": False,
        },
        timeout=30,
    )
    if resp.status_code != 200:
        log.warning(f"Tavily error {resp.status_code}: {resp.text[:200]}")
        return []
    return resp.json().get("results", [])

# ---------------------------------------------------------------------------
# Claude extraction
# ---------------------------------------------------------------------------

EXTRACTION_SYSTEM = """
You are a research assistant extracting structured use-case records from web search results about Claude AI.

For each distinct, real-world use case you find, output a JSON object on its own line (JSONL).
Only include concrete use cases — skip opinion pieces, meta-commentary, or hypothetical examples.

Schema (all fields required unless marked optional):
{
  "title":       "Short descriptive title (max 10 words)",
  "category":    "One of: Coding, Writing, Research, Legal, Healthcare, Finance, Marketing, Education, Customer Support, Strategy, Design, Other",
  "subcategory": "More specific label (optional, can be empty string)",
  "description": "One sentence summary of what Claude is doing",
  "notes":       "Free text: relevant quotes, metrics, context, links. Multiline OK.",
  "sourceUrl":   "Direct URL to the tweet, post, or article",
  "tweetDate":   "YYYY-MM-DD (best estimate from context)",
  "confidence":  8,   // 1-10: how confident you are this is a real, specific use case
  "novelty":     6    // 1-10: how novel/interesting vs common uses
}

Output ONLY valid JSONL. No preamble, no explanation.
If you find no qualifying use cases, output an empty line.
""".strip()


def extract_use_cases(search_results: list[dict]) -> list[dict]:
    """Call Claude to extract structured use cases from search result snippets."""
    if not search_results:
        return []

    content_blocks = []
    for r in search_results:
        content_blocks.append(
            f"URL: {r.get('url','')}\nTitle: {r.get('title','')}\n{r.get('content','')[:800]}"
        )
    user_message = "\n\n---\n\n".join(content_blocks)

    client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=EXTRACTION_SYSTEM,
        messages=[{"role": "user", "content": user_message}],
    )

    records = []
    for line in response.content[0].text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rec = json.loads(line)
            records.append(rec)
        except json.JSONDecodeError:
            log.warning(f"Could not parse line: {line[:100]}")

    return records

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    log.info("=== OpenClaw ingest starting ===")
    token = get_access_token()
    log.info("Obtained Firestore access token")

    all_results = []
    for q in SEARCH_QUERIES:
        log.info(f"Searching: {q}")
        results = search_web(q, max_results=8)
        all_results.extend(results)
        log.info(f"  → {len(results)} results")

    # Deduplicate search results by URL
    seen_urls: set[str] = set()
    unique_results = []
    for r in all_results:
        url = r.get("url", "")
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_results.append(r)

    log.info(f"Total unique search results: {len(unique_results)}")

    # Extract use cases via Claude
    records = extract_use_cases(unique_results)
    log.info(f"Claude extracted {len(records)} candidate records")

    inserted = 0
    skipped_dup = 0
    skipped_invalid = 0

    for rec in records:
        if inserted >= MAX_RECORDS:
            log.info(f"Reached MAX_RECORDS={MAX_RECORDS}, stopping")
            break

        source_url = rec.get("sourceUrl", "").strip()
        if not source_url or not rec.get("title"):
            skipped_invalid += 1
            continue

        if url_exists(token, source_url):
            log.info(f"  DUP  {rec['title'][:60]}")
            skipped_dup += 1
            continue

        if DRY_RUN:
            log.info(f"  DRY  {rec['title'][:60]}")
            print(json.dumps(rec, indent=2))
        else:
            try:
                doc_name = write_record(token, rec)
                log.info(f"  NEW  {rec['title'][:60]}  → {doc_name.split('/')[-1]}")
                inserted += 1
            except Exception as e:
                log.error(f"  ERR  {rec['title'][:60]}: {e}")

    log.info(
        f"=== Done. inserted={inserted} dup={skipped_dup} invalid={skipped_invalid} ==="
    )


if __name__ == "__main__":
    main()
