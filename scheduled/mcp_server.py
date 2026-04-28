#!/usr/bin/env python3
from __future__ import annotations

from mcp.server.fastmcp import FastMCP

from firestore_client import FirestoreClient

mcp = FastMCP('openclaw-db')
client = FirestoreClient()


@mcp.tool()
def search_use_cases(
    query: str = '',
    category: str = '',
    uncertainty: str = '',
    novelty: str = '',
    date_from: str = '',
    date_to: str = '',
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """Search openclaw-explorer use cases.

    Supports free-text queries, field-scoped terms like description:"tesla" or tag:agent*,
    AND/OR query logic, and direct filters for category/uncertainty/novelty/date range.
    """
    results = client.query_use_cases(
        query=query or None,
        category=category or None,
        uncertainty=uncertainty or None,
        novelty=novelty or None,
        date_from=date_from or None,
        date_to=date_to or None,
        limit=limit,
        offset=offset,
    )
    return {
        'count': len(results),
        'limit': limit,
        'offset': offset,
        'results': results,
    }


@mcp.tool()
def get_use_case(id_or_seq: str) -> dict:
    """Get a single use case by Firestore document id or integer seqId."""
    record = client.get_record(id_or_seq)
    return {'record': record}


@mcp.tool()
def get_stats() -> dict:
    """Return counts by category, novelty, uncertainty, and overall date range."""
    return client.get_stats()


@mcp.tool()
def list_categories() -> dict:
    """List categories with record counts."""
    categories = client.list_categories()
    return {'count': len(categories), 'categories': categories}


@mcp.tool()
def get_groups(status: str = '', limit: int = 20) -> dict:
    """Return resolved use_case_groups with lead + member records."""
    groups = client.get_groups(status=status or None, limit=limit)
    return {'count': len(groups), 'groups': groups}


@mcp.tool()
def get_suggestion_queue(limit: int = 20) -> dict:
    """Return the latest suggestion_queue entries."""
    queue = client.get_suggestion_queue(limit=limit)
    return {'count': len(queue), 'queue': queue}


@mcp.tool()
def refresh_cache() -> dict:
    """Invalidate FirestoreClient caches so the next call fetches fresh data."""
    return client.invalidate_cache()


if __name__ == '__main__':
    mcp.run(transport='stdio')
