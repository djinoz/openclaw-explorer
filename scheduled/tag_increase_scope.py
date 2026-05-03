#!/usr/bin/env python3
"""
Parse deletion-candidates.md and tag matching Firestore use_cases docs
with increaseScope: "<label>" so the UI can hide them by default.

Usage:
  python3 tag_increase_scope.py              # write to Firestore
  DRY_RUN=1 python3 tag_increase_scope.py   # print matches only
"""

import os
import re
import sys
from pathlib import Path

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
DRY_RUN = os.environ.get('DRY_RUN', '').strip() not in ('', '0')

CATEGORY_LABELS: dict[str, str] = {
    'A': 'News',
    'B': 'Tutorials',
    'C': 'Launches',
    'D': 'Analysis',
    'E': 'Ecosystem',
    'F': 'Duplicates',
    'G': 'Blueprints',
    'H': 'Curated',
}

MD_PATH = Path(__file__).parent / 'deletion-candidates.md'


def get_token() -> str:
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=['https://www.googleapis.com/auth/datastore'],
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def auth_headers(token: str) -> dict[str, str]:
    return {'Authorization': f'Bearer {token}', 'Content-Type': 'application/json'}


def parse_candidates(md_path: Path) -> dict[str, str]:
    """Return {normalized_url: label} from the markdown."""
    text = md_path.read_text()
    url_to_label: dict[str, str] = {}
    current_cat: str | None = None
    for line in text.splitlines():
        m = re.match(r'^## ([A-H]) —', line)
        if m:
            current_cat = m.group(1)
            continue
        if current_cat:
            for url in re.findall(r'<(https?://[^\s>]+)>', line):
                url_to_label[_norm(url)] = CATEGORY_LABELS[current_cat]
    return url_to_label


def _norm(url: str) -> str:
    return url.rstrip('/').lower()


def _parse_value(v: dict):
    if 'stringValue' in v:
        return v['stringValue']
    if 'integerValue' in v:
        return int(v['integerValue'])
    if 'arrayValue' in v:
        return [_parse_value(i) for i in v['arrayValue'].get('values', [])]
    return None


def _parse_doc(doc: dict) -> dict:
    result = {'_id': doc['name'].split('/')[-1]}
    for k, v in doc.get('fields', {}).items():
        result[k] = _parse_value(v)
    return result


def fetch_all(token: str) -> list[dict]:
    url = f'{FIRESTORE_BASE}/use_cases'
    docs, page_token = [], None
    while True:
        params: dict = {'pageSize': 300}
        if page_token:
            params['pageToken'] = page_token
        resp = requests.get(url, headers=auth_headers(token), params=params, timeout=60)
        resp.raise_for_status()
        payload = resp.json()
        docs.extend(_parse_doc(d) for d in payload.get('documents', []))
        page_token = payload.get('nextPageToken')
        if not page_token:
            break
    return docs


def record_urls(record: dict) -> list[str]:
    urls = []
    if record.get('sourceUrl'):
        urls.append(record['sourceUrl'])
    for u in re.split(r'[,\s]+', record.get('refUrls') or ''):
        u = u.strip()
        if u:
            urls.append(u)
    return urls


def patch_record(token: str, doc_id: str, label: str) -> None:
    resp = requests.patch(
        f'{FIRESTORE_BASE}/use_cases/{doc_id}',
        headers=auth_headers(token),
        params={'updateMask.fieldPaths': 'increaseScope'},
        json={'fields': {'increaseScope': {'stringValue': label}}},
        timeout=30,
    )
    resp.raise_for_status()


def main() -> None:
    candidates = parse_candidates(MD_PATH)
    print(f'Parsed {len(candidates)} candidate URLs from {MD_PATH.name}')

    token = get_token()
    records = fetch_all(token)
    print(f'Fetched {len(records)} use_cases from Firestore')

    matched = 0
    already = 0
    unmatched_candidates = set(candidates.keys())

    for rec in records:
        for url in record_urls(rec):
            label = candidates.get(_norm(url))
            if label is None:
                continue
            unmatched_candidates.discard(_norm(url))
            if rec.get('increaseScope') == label:
                already += 1
                break
            matched += 1
            print(f"  {'[DRY RUN] ' if DRY_RUN else ''}tag {rec['_id'][:8]}… → {label}  ({url[:80]})")
            if not DRY_RUN:
                patch_record(token, rec['_id'], label)
            break

    print(f'\nDone. tagged={matched}  already_tagged={already}  '
          f'unmatched_candidate_urls={len(unmatched_candidates)}')
    if unmatched_candidates:
        print('Unmatched URLs (not found in Firestore):')
        for u in sorted(unmatched_candidates):
            print(f'  {u}')


if __name__ == '__main__':
    if DRY_RUN:
        print('--- DRY RUN ---')
    main()
