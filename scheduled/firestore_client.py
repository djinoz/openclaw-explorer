#!/usr/bin/env python3
from __future__ import annotations

import datetime as dt
import fnmatch
import os
import re
from collections import Counter
from pathlib import Path
from typing import Any

import google.auth.transport.requests
import requests
from dotenv import load_dotenv
from google.oauth2 import service_account

load_dotenv(Path(__file__).parent / '.env')

FIRESTORE_PROJECT_ID = os.environ['FIRESTORE_PROJECT_ID']
CREDENTIALS_FILE = os.environ.get(
    'GOOGLE_APPLICATION_CREDENTIALS',
    str(Path(__file__).parent / 'service_account.json'),
)
FIRESTORE_BASE = (
    f'https://firestore.googleapis.com/v1/projects/{FIRESTORE_PROJECT_ID}'
    f'/databases/(default)/documents'
)

FIELD_ALIASES: dict[str, str] = {
    'id': '_id',
    'doc': '_id',
    'docid': '_id',
    'document': '_id',
    'record': '_id',
    'recordid': '_id',
    'seq': 'seqId',
    'seqid': 'seqId',
    'sequence': 'seqId',
    'category': 'category',
    'cat': 'category',
    'source': 'sourceUser',
    'sourceuser': 'sourceUser',
    'source-user': 'sourceUser',
    'user': 'sourceUser',
    'author': 'sourceUser',
    'description': 'description',
    'desc': 'description',
    'summary': 'description',
    'title': 'title',
    'headline': 'title',
    'refurls': 'refUrls',
    'refs': 'refUrls',
    'ref': 'refUrls',
    'urls': 'refUrls',
    'url': 'refUrls',
    'referenceurls': 'refUrls',
    'sourceurl': 'sourceUrl',
    'sourcelink': 'sourceUrl',
    'tweetdate': 'tweetDate',
    'tweet': 'tweetDate',
    'published': 'tweetDate',
    'publishdate': 'tweetDate',
    'date': 'tweetDate',
    'searchdate': 'searchDate',
    'ingestdate': 'searchDate',
    'notes': 'notes',
    'note': 'notes',
    'uncertainty': 'uncertainty',
    'confidence': 'confidence',
    'novelty': 'novelty',
    'newness': 'novelty',
    'subcategory': 'subcategory',
    'subcat': 'subcategory',
    'tag': 'tags',
    'tags': 'tags',
    'created': 'createdAt',
    'createdat': 'createdAt',
    'updated': 'updatedAt',
    'updatedat': 'updatedAt',
}

SEARCHABLE_FIELDS = [
    'category',
    'sourceUser',
    'description',
    'title',
    'refUrls',
    'sourceUrl',
    'tweetDate',
    'searchDate',
    'notes',
    'uncertainty',
    'novelty',
    'subcategory',
    'tags',
    '_id',
    'seqId',
]
TOKEN_RE = re.compile(r'"[^"]*"|\(|\)|\bAND\b|\bOR\b|[^\s]+', re.IGNORECASE)


def _parse_value(value: dict) -> Any:
    if 'stringValue' in value:
        return value['stringValue']
    if 'integerValue' in value:
        return int(value['integerValue'])
    if 'doubleValue' in value:
        return float(value['doubleValue'])
    if 'booleanValue' in value:
        return value['booleanValue']
    if 'timestampValue' in value:
        return value['timestampValue']
    if 'nullValue' in value:
        return None
    if 'arrayValue' in value:
        return [_parse_value(v) for v in value['arrayValue'].get('values', [])]
    if 'mapValue' in value:
        fields = value['mapValue'].get('fields', {})
        return {k: _parse_value(v) for k, v in fields.items()}
    return None


def _parse_doc(doc: dict) -> dict:
    result = {'_id': doc['name'].split('/')[-1]}
    for key, value in doc.get('fields', {}).items():
        result[key] = _parse_value(value)
    return result


def _normalize_key(key: str) -> str:
    return re.sub(r'[^a-z0-9]', '', key.lower())


def _normalize_text(value: Any) -> str:
    if value is None:
        return ''
    if isinstance(value, list):
        return ' '.join(_normalize_text(v) for v in value)
    if isinstance(value, dict):
        return ' '.join(f'{k} {_normalize_text(v)}' for k, v in value.items())
    return str(value)


def _short_date(value: Any) -> str | None:
    text = str(value or '').strip()
    if not text:
        return None
    match = re.match(r'(\d{4}-\d{2}-\d{2})', text)
    return match.group(1) if match else None


def _matches_date_filter(record_date: str | None, date_from: str | None, date_to: str | None) -> bool:
    if not date_from and not date_to:
        return True
    if not record_date:
        return False
    if date_from and record_date < date_from:
        return False
    if date_to and record_date > date_to:
        return False
    return True


class FirestoreClient:
    def __init__(self) -> None:
        self._use_cases_cache: list[dict] | None = None
        self._groups_cache: list[dict] | None = None
        self._suggestion_queue_cache: list[dict] | None = None

    def get_token(self) -> str:
        creds = service_account.Credentials.from_service_account_file(
            CREDENTIALS_FILE,
            scopes=['https://www.googleapis.com/auth/datastore'],
        )
        creds.refresh(google.auth.transport.requests.Request())
        return creds.token

    def _headers(self, token: str) -> dict[str, str]:
        return {
            'Authorization': f'Bearer {token}',
            'Content-Type': 'application/json',
        }

    def _fetch_collection(self, collection: str) -> list[dict]:
        token = self.get_token()
        url = f'{FIRESTORE_BASE}/{collection}'
        docs: list[dict] = []
        page_token: str | None = None
        while True:
            params: dict[str, Any] = {'pageSize': 300}
            if page_token:
                params['pageToken'] = page_token
            response = requests.get(url, headers=self._headers(token), params=params, timeout=60)
            if response.status_code == 404:
                return []
            response.raise_for_status()
            payload = response.json()
            docs.extend(_parse_doc(doc) for doc in payload.get('documents', []))
            page_token = payload.get('nextPageToken')
            if not page_token:
                break
        return docs

    def invalidate_cache(self) -> dict[str, bool]:
        self._use_cases_cache = None
        self._groups_cache = None
        self._suggestion_queue_cache = None
        return {
            'use_cases': True,
            'use_case_groups': True,
            'suggestion_queue': True,
        }

    def _assign_seq_ids(self, records: list[dict]) -> list[dict]:
        ordered = sorted(
            records,
            key=lambda r: (
                str(r.get('tweetDate') or ''),
                str(r.get('createdAt') or ''),
                str(r.get('_id') or ''),
            ),
        )
        for idx, record in enumerate(ordered, start=1):
            record['seqId'] = idx
        return ordered

    def use_cases(self) -> list[dict]:
        if self._use_cases_cache is None:
            self._use_cases_cache = self._assign_seq_ids(self._fetch_collection('use_cases'))
        return self._use_cases_cache

    def groups(self) -> list[dict]:
        if self._groups_cache is None:
            self._groups_cache = sorted(
                self._fetch_collection('use_case_groups'),
                key=lambda g: str(g.get('updatedAt') or g.get('createdAt') or ''),
                reverse=True,
            )
        return self._groups_cache

    def suggestion_queue(self) -> list[dict]:
        if self._suggestion_queue_cache is None:
            self._suggestion_queue_cache = sorted(
                self._fetch_collection('suggestion_queue'),
                key=lambda g: str(g.get('updatedAt') or g.get('createdAt') or ''),
                reverse=True,
            )
        return self._suggestion_queue_cache

    def _resolve_field(self, field: str) -> str | None:
        canonical = FIELD_ALIASES.get(_normalize_key(field))
        if canonical:
            return canonical
        if field in SEARCHABLE_FIELDS:
            return field
        return None

    def _tokenize_query(self, query: str) -> list[str]:
        return [tok for tok in TOKEN_RE.findall(query or '') if tok not in {'(', ')'}]

    def _split_or_clauses(self, query: str) -> list[list[str]]:
        tokens = self._tokenize_query(query)
        clauses: list[list[str]] = [[]]
        for token in tokens:
            upper = token.upper()
            if upper == 'OR':
                if clauses[-1]:
                    clauses.append([])
                continue
            if upper == 'AND':
                continue
            clauses[-1].append(token)
        return [clause for clause in clauses if clause]

    def _parse_scoped_token(self, token: str) -> tuple[str | None, str]:
        if ':' not in token:
            return None, token.strip('"')
        field, raw_value = token.split(':', 1)
        resolved = self._resolve_field(field)
        if resolved is None:
            return None, token.strip('"')
        return resolved, raw_value.strip('"')

    def _value_matches(self, haystack: Any, needle: str) -> bool:
        text = _normalize_text(haystack).lower()
        probe = needle.lower()
        if not probe:
            return True
        if '*' in probe:
            return fnmatch.fnmatch(text, probe)
        return probe in text

    def _record_matches_token(self, record: dict, token: str) -> bool:
        field, probe = self._parse_scoped_token(token)
        if not probe:
            return True
        if field:
            return self._value_matches(record.get(field), probe)
        return any(self._value_matches(record.get(candidate), probe) for candidate in SEARCHABLE_FIELDS)

    def _record_matches_query(self, record: dict, query: str | None) -> bool:
        if not query:
            return True
        for clause in self._split_or_clauses(query):
            if all(self._record_matches_token(record, token) for token in clause):
                return True
        return False

    def _normalized_filter_match(self, record_value: Any, expected: str | None) -> bool:
        if not expected:
            return True
        actual = _normalize_text(record_value).strip().lower()
        probe = expected.strip().lower()
        if not actual:
            return False
        return actual == probe or actual.startswith(probe)

    def query_use_cases(
        self,
        query: str | None = None,
        category: str | None = None,
        uncertainty: str | None = None,
        novelty: str | None = None,
        date_from: str | None = None,
        date_to: str | None = None,
        limit: int = 20,
        offset: int = 0,
    ) -> list[dict]:
        matches: list[dict] = []
        for record in self.use_cases():
            if not self._record_matches_query(record, query):
                continue
            if not self._normalized_filter_match(record.get('category'), category):
                continue
            if not self._normalized_filter_match(record.get('uncertainty'), uncertainty):
                continue
            if not self._normalized_filter_match(record.get('novelty'), novelty):
                continue
            record_date = _short_date(record.get('tweetDate')) or _short_date(record.get('searchDate'))
            if not _matches_date_filter(record_date, date_from, date_to):
                continue
            matches.append(record)
        matches.sort(
            key=lambda r: (
                str(r.get('tweetDate') or ''),
                str(r.get('searchDate') or ''),
                str(r.get('createdAt') or ''),
            ),
            reverse=True,
        )
        return matches[offset: offset + limit]

    def get_record(self, id_or_seq: str | int) -> dict | None:
        seq_lookup: int | None = None
        if isinstance(id_or_seq, int):
            seq_lookup = id_or_seq
        else:
            text = str(id_or_seq).strip()
            if text.isdigit():
                seq_lookup = int(text)
            else:
                for record in self.use_cases():
                    if record.get('_id') == text:
                        return record
                return None
        for record in self.use_cases():
            if record.get('seqId') == seq_lookup:
                return record
        return None

    def get_stats(self) -> dict[str, Any]:
        records = self.use_cases()
        categories = Counter((record.get('category') or '(unknown)').strip() or '(unknown)' for record in records)
        novelties = Counter((record.get('novelty') or '(unknown)').strip() or '(unknown)' for record in records)
        uncertainties = Counter((record.get('uncertainty') or '(unknown)').strip() or '(unknown)' for record in records)
        dates = sorted(filter(None, (_short_date(r.get('tweetDate')) for r in records)))
        return {
            'total_records': len(records),
            'by_category': dict(sorted(categories.items())),
            'by_novelty': dict(sorted(novelties.items())),
            'by_uncertainty': dict(sorted(uncertainties.items())),
            'tweet_date_range': {
                'from': dates[0] if dates else None,
                'to': dates[-1] if dates else None,
            },
            'generated_at': dt.datetime.now(dt.UTC).isoformat(),
        }

    def list_categories(self) -> list[dict[str, Any]]:
        counts = Counter((record.get('category') or '(unknown)').strip() or '(unknown)' for record in self.use_cases())
        return [
            {'category': category, 'count': count}
            for category, count in sorted(counts.items(), key=lambda item: (-item[1], item[0].lower()))
        ]

    def get_groups(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        groups = self.groups()
        records_by_id = {record['_id']: record for record in self.use_cases()}
        results: list[dict[str, Any]] = []
        for group in groups:
            if status and str(group.get('status') or '').lower() != status.lower():
                continue
            lead_id = group.get('leadId')
            member_ids = list(group.get('memberIds') or [])
            results.append({
                'id': group.get('_id'),
                'status': group.get('status'),
                'reason': group.get('reason'),
                'source': group.get('source'),
                'createdAt': group.get('createdAt'),
                'updatedAt': group.get('updatedAt'),
                'lead': records_by_id.get(lead_id),
                'members': [records_by_id[mid] for mid in member_ids if mid in records_by_id],
                'memberIds': member_ids,
            })
            if len(results) >= limit:
                break
        return results

    def get_suggestion_queue(self, limit: int = 20) -> list[dict[str, Any]]:
        return self.suggestion_queue()[:limit]
