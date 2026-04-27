"""
OpenClaw Firestore utility — read-only query client.

Provides query, filter, and search functions over the use_cases,
use_case_groups, and suggestion_queue collections.

Query syntax for search strings:
  word              → any field contains "word" (case-insensitive)
  "phrase"          → any field contains "phrase"
  field:word        → specific field contains "word"
  field:"phrase"    → specific field contains "phrase"
  agent*            → prefix wildcard (matches agent, agents, agentic…)
  *agent            → suffix wildcard
  AND / OR          → explicit operators (AND is the default)

  Field aliases: desc, cat, source, user, notes, novelty, unc,
                 date, url, urls, tags, sub, title, searchdate
"""

from __future__ import annotations

import os
import re
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import load_dotenv
from google.oauth2 import service_account
import google.auth.transport.requests

load_dotenv(Path(__file__).parent / ".env")

FIRESTORE_PROJECT_ID = os.environ["FIRESTORE_PROJECT_ID"]
CREDENTIALS_FILE = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(Path(__file__).parent / "service_account.json"),
)
FIRESTORE_BASE = (
    f"https://firestore.googleapis.com/v1/projects/{FIRESTORE_PROJECT_ID}"
    f"/databases/(default)/documents"
)

SEARCHABLE_FIELDS = [
    "category", "sourceUser", "description", "refUrls", "tweetDate",
    "searchDate", "notes", "uncertainty", "novelty", "title", "subcategory", "tags",
]

_FIELD_ALIASES: dict[str, str] = {
    "desc": "description",
    "description": "description",
    "category": "category",
    "cat": "category",
    "source": "sourceUser",
    "sourceuser": "sourceUser",
    "user": "sourceUser",
    "notes": "notes",
    "novelty": "novelty",
    "uncertainty": "uncertainty",
    "unc": "uncertainty",
    "date": "tweetDate",
    "tweetdate": "tweetDate",
    "searchdate": "searchDate",
    "tags": "tags",
    "title": "title",
    "url": "refUrls",
    "urls": "refUrls",
    "refurls": "refUrls",
    "subcategory": "subcategory",
    "sub": "subcategory",
}


# ── Auth ───────────────────────────────────────────────────────────────────────

def get_token() -> str:
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/datastore"],
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def _auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


# ── Firestore document parsing ─────────────────────────────────────────────────

def _parse_value(v: dict) -> Any:
    if "stringValue"    in v: return v["stringValue"]
    if "integerValue"   in v: return int(v["integerValue"])
    if "doubleValue"    in v: return float(v["doubleValue"])
    if "booleanValue"   in v: return v["booleanValue"]
    if "timestampValue" in v: return v["timestampValue"]
    if "nullValue"      in v: return None
    if "arrayValue"     in v:
        return [_parse_value(i) for i in v["arrayValue"].get("values", [])]
    return None


def _parse_doc(doc: dict) -> dict:
    result = {"_id": doc["name"].split("/")[-1]}
    for k, v in doc.get("fields", {}).items():
        result[k] = _parse_value(v)
    return result


# ── Collection fetch ───────────────────────────────────────────────────────────

def fetch_collection(token: str, collection: str, page_size: int = 300) -> list[dict]:
    url = f"{FIRESTORE_BASE}/{collection}"
    all_docs: list[dict] = []
    page_token: str | None = None
    while True:
        params: dict = {"pageSize": page_size}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, headers=_auth_headers(token), params=params, timeout=30)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        all_docs.extend(data.get("documents", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return [_parse_doc(d) for d in all_docs]


# ── SeqId assignment (matches app client-side ordering) ───────────────────────

def assign_seq_ids(records: list[dict]) -> list[dict]:
    sorted_recs = sorted(
        records,
        key=lambda r: (r.get("tweetDate") or "", r.get("createdAt") or ""),
    )
    for i, r in enumerate(sorted_recs):
        r["seqId"] = i + 1
    return sorted_recs


# ── Query parser ───────────────────────────────────────────────────────────────

def _tokenize_query(query: str) -> list[str]:
    """Tokenize a query string respecting quoted phrases and field:value syntax."""
    tokens: list[str] = []
    s = query.strip()
    i = 0
    while i < len(s):
        while i < len(s) and s[i].isspace():
            i += 1
        if i >= len(s):
            break

        # Scan for colon (not inside a quote) to detect field:value
        j = i
        field_colon = -1
        while j < len(s) and not s[j].isspace() and s[j] != '"':
            if s[j] == ':':
                field_colon = j
                break
            j += 1

        if field_colon != -1:
            j = field_colon + 1
            if j < len(s) and s[j] == '"':
                j += 1
                k = s.find('"', j)
                k = k if k != -1 else len(s)
                tokens.append(f'{s[i:field_colon + 1]}"{s[j:k]}"')
                i = k + 1 if k < len(s) else len(s)
            else:
                k = j
                while k < len(s) and not s[k].isspace():
                    k += 1
                tokens.append(s[i:k])
                i = k
        elif s[i] == '"':
            j = i + 1
            k = s.find('"', j)
            k = k if k != -1 else len(s)
            tokens.append(f'"{s[j:k]}"')
            i = k + 1 if k < len(s) else len(s)
        else:
            j = i
            while j < len(s) and not s[j].isspace():
                j += 1
            tokens.append(s[i:j])
            i = j

    return tokens


def _term_to_pattern(term: str) -> re.Pattern:
    """Convert a term (optionally containing * wildcards) to a compiled regex."""
    if '*' not in term:
        return re.compile(re.escape(term), re.IGNORECASE)
    parts = term.split('*')
    regex = '.*'.join(re.escape(p) for p in parts)
    return re.compile(regex, re.IGNORECASE)


def parse_query(query: str) -> list[dict]:
    """
    Parse a search query into a list of match clauses.
    Each clause: {field: str|None, pattern: re.Pattern, operator: 'AND'|'OR'}
    """
    clauses: list[dict] = []
    operator = "AND"

    for tok in _tokenize_query(query):
        if tok.upper() in ("AND", "OR"):
            operator = tok.upper()
            continue

        field: str | None = None
        value = tok

        if ":" in tok and not tok.startswith('"'):
            left, _, right = tok.partition(":")
            alias = left.lower()
            if alias in _FIELD_ALIASES:
                field = _FIELD_ALIASES[alias]
                value = right.strip('"')
        else:
            value = tok.strip('"')

        clauses.append({
            "field": field,
            "pattern": _term_to_pattern(value),
            "operator": operator,
        })
        operator = "AND"

    return clauses


def _record_haystack(record: dict, field: str | None) -> str:
    if field is None:
        parts = []
        for f in SEARCHABLE_FIELDS:
            v = record.get(f, "")
            parts.append(" ".join(str(x) for x in v) if isinstance(v, list) else str(v or ""))
        return " ".join(parts)
    v = record.get(field, "")
    return " ".join(str(x) for x in v) if isinstance(v, list) else str(v or "")


def match_record(record: dict, clauses: list[dict]) -> bool:
    if not clauses:
        return True
    result: bool | None = None
    for clause in clauses:
        matched = bool(clause["pattern"].search(_record_haystack(record, clause["field"])))
        if result is None:
            result = matched
        elif clause["operator"] == "OR":
            result = result or matched
        else:
            result = result and matched
    return bool(result)


# ── Record formatting ──────────────────────────────────────────────────────────

def format_record(r: dict) -> dict:
    """Return a clean dict for a use_case record, stripping Firestore internals."""
    return {
        "seqId":       r.get("seqId"),
        "id":          r.get("_id"),
        "category":    r.get("category"),
        "subcategory": r.get("subcategory"),
        "sourceUser":  r.get("sourceUser"),
        "description": r.get("description"),
        "refUrls":     r.get("refUrls"),
        "tweetDate":   r.get("tweetDate"),
        "searchDate":  r.get("searchDate"),
        "notes":       r.get("notes"),
        "uncertainty": r.get("uncertainty"),
        "novelty":     r.get("novelty"),
        "title":       r.get("title"),
        "tags":        r.get("tags"),
    }


# ── Client ─────────────────────────────────────────────────────────────────────

class FirestoreClient:
    """
    Read-only client for the OpenClaw Firestore database.

    Caches the use_cases collection in memory for the lifetime of the process.
    Call invalidate_cache() to force a fresh fetch on the next query.
    """

    def __init__(self) -> None:
        self._token: str | None = None
        self._token_expiry: float = 0.0
        self._use_cases: list[dict] | None = None

    # ── Auth ───────────────────────────────────────────────────────────────────

    def _fresh_token(self) -> str:
        now = time.time()
        if self._token and now < self._token_expiry - 60:
            return self._token
        self._token = get_token()
        self._token_expiry = now + 3600
        return self._token

    # ── Cache ──────────────────────────────────────────────────────────────────

    def _load_use_cases(self) -> list[dict]:
        if self._use_cases is None:
            token = self._fresh_token()
            records = fetch_collection(token, "use_cases")
            self._use_cases = assign_seq_ids(records)
        return self._use_cases

    def invalidate_cache(self) -> None:
        self._use_cases = None

    # ── Public API ─────────────────────────────────────────────────────────────

    def query_use_cases(
        self,
        query: str = "",
        *,
        category: str = "",
        uncertainty: str = "",
        novelty: str = "",
        date_from: str = "",
        date_to: str = "",
        limit: int = 50,
        offset: int = 0,
    ) -> dict:
        """
        Search and filter use cases.

        Returns {total, returned, offset, records[]}.
        All filter params are optional and combinable.
        query supports the full syntax described in the module docstring.
        """
        records = self._load_use_cases()
        clauses = parse_query(query) if query.strip() else []

        filtered: list[dict] = []
        for r in records:
            if clauses and not match_record(r, clauses):
                continue
            if category and r.get("category", "").lower() != category.lower():
                continue
            if uncertainty and r.get("uncertainty", "").lower() != uncertainty.lower():
                continue
            if novelty and novelty.lower() not in (r.get("novelty") or "").lower():
                continue
            if date_from and (r.get("tweetDate") or "") < date_from:
                continue
            if date_to and (r.get("tweetDate") or "") > date_to:
                continue
            filtered.append(r)

        total = len(filtered)
        page = filtered[offset: offset + limit]
        return {
            "total": total,
            "returned": len(page),
            "offset": offset,
            "records": [format_record(r) for r in page],
        }

    def get_record(self, id_or_seq: str | int) -> dict | None:
        """Fetch a single record by seqId (integer) or Firestore document ID (string)."""
        records = self._load_use_cases()
        if isinstance(id_or_seq, int) or (isinstance(id_or_seq, str) and id_or_seq.isdigit()):
            seq = int(id_or_seq)
            for r in records:
                if r.get("seqId") == seq:
                    return format_record(r)
            return None
        for r in records:
            if r.get("_id") == id_or_seq:
                return format_record(r)
        return None

    def get_stats(self) -> dict:
        """
        Collection statistics.

        Returns total count, breakdowns by category/novelty/uncertainty,
        and the date range of tweetDate values.
        """
        records = self._load_use_cases()
        by_category: dict[str, int] = {}
        by_novelty: dict[str, int] = {}
        by_uncertainty: dict[str, int] = {}
        dates: list[str] = []

        for r in records:
            cat = r.get("category") or "(unknown)"
            by_category[cat] = by_category.get(cat, 0) + 1

            nov = r.get("novelty") or "(none)"
            by_novelty[nov] = by_novelty.get(nov, 0) + 1

            unc = r.get("uncertainty") or "(none)"
            by_uncertainty[unc] = by_uncertainty.get(unc, 0) + 1

            td = r.get("tweetDate")
            if td:
                dates.append(td)

        dates.sort()
        return {
            "total": len(records),
            "by_category":    dict(sorted(by_category.items(),    key=lambda x: -x[1])),
            "by_novelty":     dict(sorted(by_novelty.items(),     key=lambda x: -x[1])),
            "by_uncertainty": dict(sorted(by_uncertainty.items(), key=lambda x: -x[1])),
            "date_range": {
                "earliest": dates[0]  if dates else None,
                "latest":   dates[-1] if dates else None,
            },
        }

    def list_categories(self) -> list[str]:
        """Return a sorted list of all distinct category values."""
        records = self._load_use_cases()
        return sorted({r.get("category") for r in records if r.get("category")})

    def get_groups(self, status: str = "", limit: int = 50) -> dict:
        """
        Fetch similarity groups with lead and member records resolved.

        status: filter by 'pending', 'approved', or 'rejected'. Empty = all.
        Returns {total, groups[]} where each group has lead/members records.
        """
        token = self._fresh_token()
        records = self._load_use_cases()
        doc_map = {r["_id"]: r for r in records}

        groups = fetch_collection(token, "use_case_groups")
        if status:
            groups = [g for g in groups if g.get("status", "").lower() == status.lower()]

        result: list[dict] = []
        for g in groups[:limit]:
            lead = doc_map.get(g.get("leadId", ""))
            members = [
                doc_map[mid]
                for mid in (g.get("memberIds") or [])
                if mid in doc_map
            ]
            result.append({
                "id":           g["_id"],
                "status":       g.get("status"),
                "reason":       g.get("reason"),
                "member_count": len(members),
                "lead":         format_record(lead) if lead else None,
                "members":      [format_record(m) for m in members],
            })

        return {"total": len(result), "groups": result}

    def get_suggestion_queue(self, status: str = "pending", limit: int = 20) -> dict:
        """
        Fetch submitted URL suggestions.

        status: 'pending' (default), 'approved', 'rejected', or '' for all.
        Returns {total, returned, items[]}.
        """
        token = self._fresh_token()
        items = fetch_collection(token, "suggestion_queue")
        if status:
            items = [i for i in items if i.get("status", "").lower() == status.lower()]
        items.sort(key=lambda x: x.get("createdAt") or "", reverse=True)
        page = items[:limit]
        return {
            "total":    len(items),
            "returned": len(page),
            "items": [
                {
                    "id":          i["_id"],
                    "url":         i.get("url"),
                    "displayName": i.get("displayName"),
                    "creditMode":  i.get("creditMode"),
                    "status":      i.get("status"),
                    "createdAt":   i.get("createdAt"),
                }
                for i in page
            ],
        }
