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
import hashlib
import re
import logging
import secrets
from datetime import datetime, timezone
from datetime import timedelta
from urllib.parse import urlsplit, urlunsplit

import requests
import anthropic
from firebase_admin import initialize_app, firestore
from firebase_admin import auth as fb_auth
from firebase_functions import scheduler_fn, https_fn, options

# Initialise Firebase Admin (uses Application Default Credentials automatically)
initialize_app()

logger = logging.getLogger(__name__)

COLLECTION = "use_cases"
SUGGESTION_COLLECTION = "suggestion_queue"
SESSION_COLLECTION = "suggestion_sessions"
LIMIT_COLLECTION = "suggestion_submission_limits"
MAX_RECORDS = 20
ALLOWED_FIELDS = {
    "category", "sourceUser", "description", "refUrls", "tweetDate",
    "notes", "uncertainty", "novelty", "searchDate", "createdAt",
    "updatedAt", "tags", "title", "sourceUrl", "subcategory",
    "confidence",
}

SUGGESTION_CREDIT_MODES = {"profile", "nickname", "anonymous"}


def cors_headers(origin: str | None = None) -> dict[str, str]:
    return {
        "Access-Control-Allow-Origin": origin or "*",
        "Access-Control-Allow-Headers": "Content-Type, Authorization",
        "Access-Control-Allow-Methods": "POST, OPTIONS",
        "Vary": "Origin",
    }


def json_response(payload: dict, status: int = 200, origin: str | None = None):
    return payload, status, cors_headers(origin)


def read_request_data(req) -> dict:
    try:
        return req.get_json(silent=True) or {}
    except Exception:
        return {}


def read_bearer_token(req) -> str | None:
    auth_header = req.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        token = auth_header.removeprefix("Bearer ").strip()
        return token or None
    return None


def decode_request_user(req) -> dict | None:
    token = read_bearer_token(req)
    if not token:
        return None
    try:
        return fb_auth.verify_id_token(token)
    except Exception:
        return None

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
    memory=options.MemoryOption.MB_512,
    timeout_sec=300,
)
def daily_ingest(event: scheduler_fn.ScheduledEvent) -> None:
    """Triggered daily at 08:00 Brisbane time."""
    import os
    anthropic_key = os.environ.get("ANTHROPIC_API_KEY")
    search_key    = os.environ.get("SEARCH_API_KEY")
    if not anthropic_key or not search_key:
        logger.warning("Skipping daily ingest because Anthropic or search API key is missing.")
        return
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


def normalize_suggestion_url(value: str) -> str | None:
    from urllib.parse import urlsplit, urlunsplit

    if not isinstance(value, str):
        return None
    candidate = value.strip()
    if not candidate:
        return None
    try:
        parsed = urlsplit(candidate)
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"}:
        return None
    if not parsed.netloc:
        return None
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/") or "/"
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))


def clean_display_name(value: object) -> str:
    text = "" if value is None else str(value).strip()
    return text[:80]


def browser_hash(browser_id: str) -> str:
    return hashlib.sha256(browser_id.encode("utf-8")).hexdigest()


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def limit_ref(db, kind: str, identifier: str):
    return db.collection(LIMIT_COLLECTION).document(f"{kind}:{identifier}")


def suggestion_session_ref(db, browser_id: str):
    return db.collection(SESSION_COLLECTION).document(browser_hash(browser_id))


def too_soon(previous: datetime | None, seconds: int) -> bool:
    if previous is None:
        return False
    return (utc_now() - previous) < timedelta(seconds=seconds)


def session_payload(browser_id: str, token: str) -> dict:
    now = utc_now()
    return {
        "browserHash": browser_hash(browser_id),
        "browserId": browser_id,
        "token": token,
        "createdAt": now,
        "expiresAt": now + timedelta(minutes=30),
        "usedAt": None,
    }


@https_fn.on_request()
def requestSuggestionSessionHttp(req):
    if req.method == "OPTIONS":
        return json_response({}, 204, req.headers.get("Origin"))
    return json_response(requestSuggestionSessionCore(read_request_data(req), None), 200, req.headers.get("Origin"))


def requestSuggestionSessionCore(data: dict, _auth_claims: dict | None):
    browser_id = str(data.get("browserId", "")).strip()
    if not browser_id:
        return {"ok": False, "error": "Missing browserId."}

    db = firestore.client()
    limit = limit_ref(db, "anonymous-session", browser_hash(browser_id))
    snapshot = limit.get().to_dict() or {}
    last_issued = snapshot.get("lastIssuedAt")
    if isinstance(last_issued, datetime) and too_soon(last_issued, 15):
        existing = suggestion_session_ref(db, browser_id).get().to_dict() or {}
        if existing and existing.get("usedAt") is None and isinstance(existing.get("expiresAt"), datetime) and existing["expiresAt"] > utc_now():
            return {
                "ok": True,
                "sessionId": browser_hash(browser_id),
                "sessionToken": existing.get("token"),
                "expiresAt": existing["expiresAt"].isoformat(),
            }
        return {"ok": False, "error": "Please wait a moment before requesting another anonymous session."}

    token = secrets.token_urlsafe(24)
    payload = session_payload(browser_id, token)
    suggestion_session_ref(db, browser_id).set(payload)
    limit.set({"lastIssuedAt": utc_now()}, merge=True)
    return {
        "ok": True,
        "sessionId": payload["browserHash"],
        "sessionToken": token,
        "expiresAt": payload["expiresAt"].isoformat(),
    }
@https_fn.on_request()
def submitSuggestionHttp(req):
    if req.method == "OPTIONS":
        return json_response({}, 204, req.headers.get("Origin"))
    return json_response(submitSuggestionCore(read_request_data(req), decode_request_user(req)), 200, req.headers.get("Origin"))


def submitSuggestionCore(data: dict, auth_claims: dict | None):
    url = normalize_suggestion_url(data.get("url"))
    if not url:
        return {"ok": False, "error": "Please provide a valid http(s) URL."}

    credit_mode = str(data.get("creditMode", "anonymous")).strip().lower()
    if credit_mode not in SUGGESTION_CREDIT_MODES:
        credit_mode = "anonymous"

    honeypot = str(data.get("honeypot", "")).strip()
    if honeypot:
        return {"ok": False, "error": "Submission blocked."}

    form_age_ms = int(data.get("formAgeMs", 0) or 0)
    if form_age_ms and form_age_ms < 1500:
        return {"ok": False, "error": "Please take a moment before submitting."}

    public_name = clean_display_name(data.get("displayName"))
    auth_uid = auth_claims.get("uid") if auth_claims else None
    if auth_claims and credit_mode == "profile":
        public_name = clean_display_name(auth_claims.get("name") or auth_claims.get("email") or "Contributor")
    elif not public_name and credit_mode == "nickname":
        public_name = "Contributor"
    elif credit_mode == "anonymous":
        public_name = "Anonymous"

    db = firestore.client()
    now = utc_now()
    browser_id = str(data.get("browserId", "")).strip()
    submission_limit_key = auth_uid or browser_hash(browser_id or public_name)
    limit_doc = limit_ref(db, "submission", submission_limit_key)
    limit_state = limit_doc.get().to_dict() or {}
    last_submitted = limit_state.get("lastSubmittedAt")
    if isinstance(last_submitted, datetime) and too_soon(last_submitted, 30):
        return {"ok": False, "error": "Please wait before submitting another suggestion."}

    if auth_claims:
        actor_type = "logged_in"
    else:
        session_id = str(data.get("sessionId", "")).strip()
        session_token = str(data.get("sessionToken", "")).strip()
        if not browser_id or not session_id or not session_token:
            return {"ok": False, "error": "Anonymous suggestions require a valid session."}

        session_ref = suggestion_session_ref(db, browser_id)
        session = session_ref.get().to_dict()
        if not session:
            return {"ok": False, "error": "Anonymous session not found."}
        if session.get("browserHash") != session_id or session.get("token") != session_token:
            return {"ok": False, "error": "Anonymous session is invalid."}
        expires_at = session.get("expiresAt")
        if not isinstance(expires_at, datetime) or expires_at <= now:
            return {"ok": False, "error": "Anonymous session expired. Please refresh and try again."}
        if session.get("usedAt") is not None:
            return {"ok": False, "error": "This anonymous session has already been used."}

        session_ref.update({"usedAt": now})
        actor_type = "anonymous"

    doc_ref = db.collection(SUGGESTION_COLLECTION).document()
    doc_ref.set({
        "url": url,
        "normalizedUrl": url,
        "displayName": public_name,
        "creditMode": credit_mode,
        "submittedByType": actor_type,
        "submittedByUid": auth_uid,
        "status": "pending",
        "createdAt": now,
        "updatedAt": now,
        "source": "web",
    })
    limit_doc.set({"lastSubmittedAt": now}, merge=True)
    return {
        "ok": True,
        "id": doc_ref.id,
        "status": "pending",
        "displayName": public_name,
    }
