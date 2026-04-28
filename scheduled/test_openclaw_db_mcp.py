#!/usr/bin/env python3
from __future__ import annotations

import json

from firestore_client import FirestoreClient


client = FirestoreClient()
results = client.query_use_cases(
    category='Productivity',
    novelty='high',
    date_from='2026-04-01',
    date_to='2026-04-30',
    limit=200,
    offset=0,
)

for record in results:
    category = str(record.get('category') or '')
    novelty = str(record.get('novelty') or '')
    tweet_date = str(record.get('tweetDate') or record.get('searchDate') or '')
    assert category.lower().startswith('productivity'), record
    assert novelty.lower().startswith('high'), record
    assert tweet_date[:10] >= '2026-04-01', record
    assert tweet_date[:10] <= '2026-04-30', record

payload = {
    'count': len(results),
    'seqIds': [record.get('seqId') for record in results],
    'ids': [record.get('_id') for record in results],
    'titles': [record.get('title') or record.get('description') for record in results],
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
