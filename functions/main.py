"""
OpenClaw daily use-case ingest — Firebase Cloud Function
=========================================================
Runs daily via Cloud Scheduler. Searches the web for new Claude/OpenClaw
use cases, extracts structured records with the Claude API, deduplicates
against Firestore, and writes new records.

Secrets (set once via CLI, never in code):
  firebase functions:secrets:set ANTHROPIC_API_KEY
  firebase functions:secrets:set SEARCH_API_KEY

Deploy:
  firebase deploy --only functions
"""

import json
import re
import logging
from datetime import datetime, timezone

import requests
import anthropic
from firebase_admin import initialize_app, firestore
from firebase_functions import scheduler_fn, options

# Initialise Firebase Admin (uses Application Default Credentials automatically)
initialize_app()

logger = logging.getLogger(__name__)

COLLECTION = "use_cases"
MAX_RECORDS = 20
ALLOWED_FIELDS = {
    "category", "sourceUser", "description", "refUrls", "tweetDate",
    "notes", "uncertainty", "novelty", "searchDate", "createdAt",
    "updatedAt", "tags", "title", "sourceUrl", "subcategory",
    "confidence",
}

SEARCH_QUERIES = [
    "Claude AI real world use case 2025",
    "Anthropic Claude enterprise deployment example",
    '"using Claude" OR "built with Claude" AI use case',
    "openclaw browser agent use case example",
]

EXTRACTION_SYSTEM = """
You are a research assistant extracting structured use-case records from web
search results about Claude AI and OpenClaw (an AI browser agent).

For each distinct, real-world use case output a JSON object on its own line (JSONL).
Only include concrete use cases — skip opinion pieces, meta-commentary, or hypotheticals.

Schema (all fields required unless noted):
{
  "category":    "One of: Coding, Writing, Research, Legal, Healthcare, Finance, Marketing, Education, Customer Support, Strategy, Design, Productivity, Other",
  "sourceUser":  "Twitter handle or author name, or 'unknown'",
  "description": "One sentence summary of what Claude/OpenClaw is doing",
  "refUrls":     "Direct URL to the source (tweet, post, article)",
  "tweetDate":   "YYYY-MM-DD (best estimate)",
  "notes":       "Free text: relevant quotes, metrics, context. Multiline OK.",
  "uncertainty": "low | medium | high  (how confident this is a real specific use case)",
  "novelty":     "low | medium | high  (how novel vs common)"
}

Output ONLY valid JSONL. No preamble. If no qualifying use cases found, output nothing.
""".strip()


# ── Scheduled entry point ──────────────────────────────────────────────────────

@scheduler_fn.on_schedule(
    schedule="every day 08:00",
    timezone=scheduler_fn.Timezone("Australia/Brisbane"),
    secrets=["ANTHROPIC_API_KEY", "SEARCH_API_KEY"],
    memory=options.MemoryOption.MB_512,
    timeout_sec=300,
)
def daily_ingest(event: scheduler_fn.ScheduledEvent) -> None:
    """Triggered daily at 08:00 Brisbane time."""
    import os
    anthropic_key = os.environ["ANTHROPIC_API_KEY"]
    search_key    = os.environ["SEARCH_API_KEY"]
    run(anthropic_key, search_key)


# ── Core logic ────────────────────────────────────────────────────────────────

def run(anthropic_key: str, search_key: str) -> dict:
    db = firestore.client()
    col = db.collection(COLLECTION)

    # 1. Web search
    all_results = []
    for q in SEARCH_QUERIES:
        results = search_web(q, search_key, max_results=8)
        all_results.extend(results)
        logger.info(f"Query '{q[:40]}…' → {len(results)} results")

    # Deduplicate by URL
    seen, unique = set(), []
    for r in all_results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            unique.append(r)
    logger.info(f"Unique search results: {len(unique)}")

    # 2. Extract records with Claude
    candidates = extract_use_cases(unique, anthropic_key)
    logger.info(f"Claude extracted {len(candidates)} candidates")

    # 3. Deduplicate against Firestore and write
    inserted = skipped = invalid = 0
    for rec in candidates:
        if inserted >= MAX_RECORDS:
            break
        clean = normalize_record(rec)
        if not clean or not clean.get("description"):
            invalid += 1
            continue
        url = clean.get("refUrls", "").strip()
        # Check for existing record with same URL
        existing = col.where("refUrls", "==", url).limit(1).get()
        if len(list(existing)) > 0:
            skipped += 1
            continue
        col.add({
            **clean,
            "searchDate": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "createdAt":  firestore.SERVER_TIMESTAMP,
            "updatedAt":  firestore.SERVER_TIMESTAMP,
        })
        inserted += 1
        logger.info(f"  NEW  {clean.get('description','')[:60]}")

    summary = f"inserted={inserted} dupes={skipped} invalid={invalid}"
    logger.info(f"Done. {summary}")
    return {"status": "ok", "summary": summary}


# ── Web search (Tavily) ────────────────────────────────────────────────────────

def search_web(query: str, api_key: str, max_results: int = 8) -> list[dict]:
    try:
        resp = requests.post(
            "https://api.tavily.com/search",
            json={
                "api_key": api_key,
                "query": query,
                "search_depth": "basic",
                "max_results": max_results,
                "include_raw_content": False,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json().get("results", [])
    except Exception as e:
        logger.warning(f"Search error: {e}")
        return []


# ── Claude extraction ──────────────────────────────────────────────────────────

def extract_use_cases(results: list[dict], api_key: str) -> list[dict]:
    if not results:
        return []
    blocks = []
    for r in results:
        blocks.append(
            f"URL: {r.get('url','')}\n"
            f"Title: {r.get('title','')}\n"
            f"{r.get('content','')[:800]}"
        )
    user_msg = "\n\n---\n\n".join(blocks)

    client = anthropic.Anthropic(api_key=api_key)
    response = client.messages.create(
        model="claude-opus-4-6",
        max_tokens=4096,
        system=EXTRACTION_SYSTEM,
        messages=[{"role": "user", "content": user_msg}],
    )

    records = []
    for line in response.content[0].text.strip().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            records.append(json.loads(line))
        except json.JSONDecodeError:
            logger.warning(f"Could not parse: {line[:80]}")
    return records


def safe_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


def normalize_record(record: dict) -> dict | None:
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
