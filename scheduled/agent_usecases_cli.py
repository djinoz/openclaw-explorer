#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from firestore_client import CREDENTIALS_FILE, FIRESTORE_PROJECT_ID, FirestoreClient

REPO_ROOT = Path(__file__).resolve().parents[1]


def _first_url(record: dict[str, Any]) -> str:
    ref_urls = record.get("refUrls")
    if isinstance(ref_urls, list):
        for item in ref_urls:
            if item:
                return str(item)
    elif isinstance(ref_urls, str) and ref_urls.strip():
        return ref_urls.strip()
    source_url = str(record.get("sourceUrl") or "").strip()
    return source_url


def _title_for_record(record: dict[str, Any]) -> str:
    title = str(record.get("title") or "").strip()
    if title:
        return title
    description = " ".join(str(record.get(key) or "").strip() for key in ("description", "notes")).strip()
    if description:
        compact = " ".join(description.split())
        return compact[:117] + "..." if len(compact) > 120 else compact
    seq = record.get("seqId")
    return f"OpenClaw use case {seq}" if seq else f"OpenClaw use case {record.get('_id', 'unknown')}"


def _body_for_record(record: dict[str, Any]) -> str:
    parts: list[str] = []
    description = str(record.get("description") or "").strip()
    if description:
        parts.append(description)
    notes = str(record.get("notes") or "").strip()
    if notes:
        parts.append(f"Notes: {notes}")
    tags = record.get("tags") or []
    if isinstance(tags, list) and tags:
        parts.append("Tags: " + ", ".join(str(tag) for tag in tags if tag))
    elif isinstance(tags, str) and tags.strip():
        parts.append(f"Tags: {tags.strip()}")
    return "\n\n".join(parts)


def _last30days_item(record: dict[str, Any]) -> dict[str, Any]:
    title = _title_for_record(record)
    body = _body_for_record(record)
    published_at = record.get("tweetDate") or record.get("searchDate") or record.get("createdAt")
    category = str(record.get("category") or "").strip()
    novelty = str(record.get("novelty") or "").strip().lower()
    uncertainty = str(record.get("uncertainty") or record.get("confidence") or "").strip().lower()

    novelty_hint = {
        "high": 0.88,
        "medium": 0.72,
        "low": 0.58,
    }.get(novelty, 0.62)

    why_bits = [bit for bit in [category, novelty, uncertainty] if bit]
    why_relevant = "Agent use case landscape record"
    if why_bits:
        why_relevant += f" ({', '.join(why_bits)})"

    return {
        "item_id": str(record.get("_id") or record.get("seqId") or title),
        "source": "agent_usecases",
        "title": title,
        "body": body,
        "url": _first_url(record),
        "author": record.get("sourceUser") or None,
        "container": record.get("category") or None,
        "published_at": published_at,
        "date_confidence": "med" if published_at else "low",
        "engagement": {
            "records": 1,
            "seq_id": record.get("seqId") or 0,
        },
        "relevance_hint": novelty_hint,
        "why_relevant": why_relevant,
        "snippet": (body or title)[:280],
        "metadata": {
            "doc_id": record.get("_id"),
            "seq_id": record.get("seqId"),
            "category": record.get("category"),
            "subcategory": record.get("subcategory"),
            "novelty": record.get("novelty"),
            "uncertainty": record.get("uncertainty") or record.get("confidence"),
            "search_date": record.get("searchDate"),
            "tweet_date": record.get("tweetDate"),
            "source_url": record.get("sourceUrl"),
            "ref_urls": record.get("refUrls"),
            "tags": record.get("tags"),
            "backend": "openclaw-explorer",
        },
    }


def _json_dump(payload: Any, pretty: bool = False) -> None:
    json.dump(payload, sys.stdout, indent=2 if pretty else None, sort_keys=pretty)
    sys.stdout.write("\n")


def _search_payload(client: FirestoreClient, args: argparse.Namespace) -> dict[str, Any]:
    records = client.query_use_cases(
        query=args.query or None,
        category=args.category or None,
        uncertainty=args.uncertainty or None,
        novelty=args.novelty or None,
        date_from=args.date_from or None,
        date_to=args.date_to or None,
        limit=args.limit,
        offset=args.offset,
    )
    return {
        "count": len(records),
        "limit": args.limit,
        "offset": args.offset,
        "results": records,
    }


def run_cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent-usecases",
        description="Query the agent use case landscape data from the terminal or via MCP.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    search_parser = subparsers.add_parser("search", help="Search use cases")
    search_parser.add_argument("query", nargs="?", default="")
    search_parser.add_argument("--category", default="")
    search_parser.add_argument("--uncertainty", default="")
    search_parser.add_argument("--novelty", default="")
    search_parser.add_argument("--date-from", default="")
    search_parser.add_argument("--date-to", default="")
    search_parser.add_argument("--limit", type=int, default=20)
    search_parser.add_argument("--offset", type=int, default=0)
    search_parser.add_argument("--pretty", action="store_true")

    get_parser = subparsers.add_parser("get", help="Get one use case by doc id or seq id")
    get_parser.add_argument("id_or_seq")
    get_parser.add_argument("--pretty", action="store_true")

    stats_parser = subparsers.add_parser("stats", help="Get aggregate stats")
    stats_parser.add_argument("--pretty", action="store_true")

    categories_parser = subparsers.add_parser("categories", help="List categories")
    categories_parser.add_argument("--pretty", action="store_true")

    groups_parser = subparsers.add_parser("groups", help="List use case groups")
    groups_parser.add_argument("--status", default="")
    groups_parser.add_argument("--limit", type=int, default=20)
    groups_parser.add_argument("--pretty", action="store_true")

    queue_parser = subparsers.add_parser("queue", help="List suggestion queue entries")
    queue_parser.add_argument("--limit", type=int, default=20)
    queue_parser.add_argument("--pretty", action="store_true")

    refresh_parser = subparsers.add_parser("refresh-cache", help="Invalidate local Firestore cache")
    refresh_parser.add_argument("--pretty", action="store_true")

    doctor_parser = subparsers.add_parser("doctor", help="Smoke-test credentials and connectivity")
    doctor_parser.add_argument("--pretty", action="store_true")

    l30_parser = subparsers.add_parser(
        "last30days-source",
        help="Emit normalized evidence items for last30days or other agent tools",
    )
    l30_parser.add_argument("topic")
    l30_parser.add_argument("--date-from", default="")
    l30_parser.add_argument("--date-to", default="")
    l30_parser.add_argument("--category", default="")
    l30_parser.add_argument("--uncertainty", default="")
    l30_parser.add_argument("--novelty", default="")
    l30_parser.add_argument("--limit", type=int, default=12)
    l30_parser.add_argument("--offset", type=int, default=0)
    l30_parser.add_argument("--pretty", action="store_true")

    subparsers.add_parser("mcp", help="Run the stdio MCP server")

    args = parser.parse_args(argv)

    if args.command == "mcp":
        from mcp_server import mcp

        mcp.run(transport="stdio")
        return 0

    client = FirestoreClient()

    if args.command == "search":
        _json_dump(_search_payload(client, args), pretty=args.pretty)
        return 0
    if args.command == "get":
        _json_dump({"record": client.get_record(args.id_or_seq)}, pretty=args.pretty)
        return 0
    if args.command == "stats":
        _json_dump(client.get_stats(), pretty=args.pretty)
        return 0
    if args.command == "categories":
        categories = client.list_categories()
        _json_dump({"count": len(categories), "categories": categories}, pretty=args.pretty)
        return 0
    if args.command == "groups":
        groups = client.get_groups(status=args.status or None, limit=args.limit)
        _json_dump({"count": len(groups), "groups": groups}, pretty=args.pretty)
        return 0
    if args.command == "queue":
        queue = client.get_suggestion_queue(limit=args.limit)
        _json_dump({"count": len(queue), "queue": queue}, pretty=args.pretty)
        return 0
    if args.command == "refresh-cache":
        _json_dump(client.invalidate_cache(), pretty=args.pretty)
        return 0
    if args.command == "doctor":
        payload = {
            "repo_root": str(REPO_ROOT),
            "credential_file": str(Path(CREDENTIALS_FILE)),
            "project_id": FIRESTORE_PROJECT_ID,
            "stats": client.get_stats(),
        }
        _json_dump(payload, pretty=args.pretty)
        return 0
    if args.command == "last30days-source":
        records = client.query_use_cases(
            query=args.topic or None,
            category=args.category or None,
            uncertainty=args.uncertainty or None,
            novelty=args.novelty or None,
            date_from=args.date_from or None,
            date_to=args.date_to or None,
            limit=args.limit,
            offset=args.offset,
        )
        payload = {
            "count": len(records),
            "items": [_last30days_item(record) for record in records],
            "records": records,
        }
        _json_dump(payload, pretty=args.pretty)
        return 0

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(run_cli())
