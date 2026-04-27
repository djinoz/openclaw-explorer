#!/usr/bin/env python3
"""
OpenClaw MCP server — read-only Firestore query tool for AI agents.

Exposes the OpenClaw use-case database as MCP tools so that agents
(Hermes, OpenClaw, Claude Code, etc.) can query, filter, and reason
over records conversationally.

Usage (stdio transport, default):
  python mcp_server.py

Agent configuration example:
  {
    "mcpServers": {
      "openclaw-db": {
        "command": "/path/to/openclaw-explorer/scheduled/venv/bin/python",
        "args": ["/path/to/openclaw-explorer/scheduled/mcp_server.py"]
      }
    }
  }

Environment (loaded from .env in the same directory):
  GOOGLE_APPLICATION_CREDENTIALS   path to service_account.json
  FIRESTORE_PROJECT_ID              Firebase project ID
"""

from __future__ import annotations

import json
from typing import Annotated

from mcp.server.fastmcp import FastMCP

from firestore_client import FirestoreClient

# Single client instance — caches the use_cases collection across tool calls.
_client = FirestoreClient()

mcp = FastMCP(
    "openclaw-db",
    instructions=(
        "Query the OpenClaw use-case database — a curated collection of real-world "
        "Claude / AI-agent deployments. Use search_use_cases to find and filter records, "
        "get_use_case for individual record detail, get_stats for an overview, "
        "get_groups to explore similarity clusters, and get_suggestion_queue for "
        "pending submissions. All tools are read-only."
    ),
)


# ── Tools ──────────────────────────────────────────────────────────────────────

@mcp.tool()
def search_use_cases(
    query: Annotated[str, "Search query. Supports field scoping and wildcards. "
                          "Examples: 'tesla', description:\"contract review\", "
                          "'legal AND agent*', source:anthropic OR source:openai"] = "",
    category: Annotated[str, "Exact category filter, e.g. 'Engineering', 'Finance', 'Legal'. "
                              "Leave empty to search all categories."] = "",
    uncertainty: Annotated[str, "Filter by uncertainty: 'low', 'medium', or 'high'."] = "",
    novelty: Annotated[str, "Filter by novelty: 'highly novel', 'novel', 'possibly novel', 'common'. "
                            "Partial match — 'novel' matches 'highly novel' and 'novel'."] = "",
    date_from: Annotated[str, "Earliest tweetDate to include, YYYY-MM-DD format."] = "",
    date_to: Annotated[str, "Latest tweetDate to include, YYYY-MM-DD format."] = "",
    limit: Annotated[int, "Maximum records to return (1–200)."] = 50,
    offset: Annotated[int, "Pagination offset."] = 0,
) -> str:
    """
    Search and filter the use_cases collection.

    Query syntax:
      word              matches any field (case-insensitive)
      "exact phrase"    phrase match
      field:word        field-scoped match (field aliases: desc, cat, source/user,
                        notes, novelty, unc, date, url, tags, sub, title)
      field:"phrase"    field-scoped phrase
      agent*            prefix wildcard  → agent, agents, agentic…
      *agent            suffix wildcard
      AND / OR          boolean operators (AND is default between terms)

    Examples:
      "legal contract"
      description:"code review" AND novelty:novel
      source:anthropic OR source:openai
      cat:Finance date_from:2025-01-01

    Returns JSON with total count, pagination info, and matching records.
    Each record includes seqId, id, category, sourceUser, description,
    refUrls, tweetDate, notes, uncertainty, novelty, and optional fields.
    """
    limit = max(1, min(limit, 200))
    result = _client.query_use_cases(
        query=query,
        category=category,
        uncertainty=uncertainty,
        novelty=novelty,
        date_from=date_from,
        date_to=date_to,
        limit=limit,
        offset=offset,
    )
    return json.dumps(result, default=str)


@mcp.tool()
def get_use_case(
    id_or_seq: Annotated[str, "A seqId integer (e.g. '42') or a Firestore document ID string."],
) -> str:
    """
    Fetch the full detail of a single use_case record.

    seqId values are stable sequential integers assigned by sort order
    (tweetDate, then createdAt). They match what the OpenClaw Explorer
    UI shows as the record number (#42, etc.).

    Returns the full record as JSON, or {"error": "not found"} if the
    ID does not exist.
    """
    record = _client.get_record(id_or_seq)
    if record is None:
        return json.dumps({"error": f"Record not found: {id_or_seq}"})
    return json.dumps(record, default=str)


@mcp.tool()
def get_stats() -> str:
    """
    Return aggregate statistics for the use_cases collection.

    Includes:
    - total record count
    - breakdown by category (sorted by count)
    - breakdown by novelty level
    - breakdown by uncertainty level
    - date range of tweetDate values (earliest and latest)

    Useful for understanding the shape of the dataset before querying.
    """
    stats = _client.get_stats()
    return json.dumps(stats, default=str)


@mcp.tool()
def list_categories() -> str:
    """
    Return a sorted list of all distinct category values in use_cases.

    Use this to discover valid category names before filtering with
    search_use_cases(category=...).
    """
    cats = _client.list_categories()
    return json.dumps({"categories": cats})


@mcp.tool()
def get_groups(
    status: Annotated[str, "Filter by group status: 'pending', 'approved', or 'rejected'. "
                           "Leave empty to return all groups."] = "",
    limit: Annotated[int, "Maximum number of groups to return (1–100)."] = 50,
) -> str:
    """
    Fetch similarity groups with their lead and member records resolved.

    The use_case_groups collection is populated by find_similars.py,
    which uses Claude to cluster near-duplicate or related records.
    Groups have a status of 'pending' (awaiting owner review),
    'approved', or 'rejected'.

    Each group in the response includes:
    - id, status, reason  (why the records were grouped)
    - lead record (full record detail)
    - members (list of full record details)
    - member_count

    Useful for understanding which use cases are considered duplicates
    or closely related by the automated similarity analysis.
    """
    limit = max(1, min(limit, 100))
    result = _client.get_groups(status=status, limit=limit)
    return json.dumps(result, default=str)


@mcp.tool()
def get_suggestion_queue(
    limit: Annotated[int, "Maximum number of suggestions to return (1–100)."] = 20,
) -> str:
    """
    Fetch pending URL suggestions submitted by users via the web app.

    Suggestions are user-submitted URLs awaiting owner review. Each item
    includes the submitted URL, submitter display name, credit mode
    (profile/nickname/anonymous), and submission timestamp.

    Returns the most recently submitted items first.
    """
    limit = max(1, min(limit, 100))
    result = _client.get_suggestion_queue(status="pending", limit=limit)
    return json.dumps(result, default=str)


@mcp.tool()
def refresh_cache() -> str:
    """
    Invalidate the in-memory use_cases cache.

    The server caches the use_cases collection on first access and reuses
    it across tool calls for performance. Call this if you know records
    have been added or changed and want the next query to fetch fresh data.
    """
    _client.invalidate_cache()
    return json.dumps({"ok": True, "message": "Cache cleared. Next query will fetch fresh data."})


# ── Entry point ────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    mcp.run()
