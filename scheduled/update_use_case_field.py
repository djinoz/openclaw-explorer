#!/usr/bin/env python3
"""
Patch a single Firestore use_cases record field by seqId or document id.

Examples:
  python3 update_use_case_field.py --seq-id 912 --field increaseScope --value Tutorials --dry-run
  python3 update_use_case_field.py --doc-id MBWPUePmqN9XtihLHCip --field title --value "New title"
  python3 update_use_case_field.py --seq-id 912 --field tags --json-value '["agent", "workflow"]'

Environment variables (in .env):
  GOOGLE_APPLICATION_CREDENTIALS   path to service_account.json
  FIRESTORE_PROJECT_ID            Firebase project ID

Safety:
  - patches exactly one user-specified field plus updatedAt
  - restricted to known use_cases fields
  - defaults to string values unless --type/--json-value is provided
"""

from __future__ import annotations

import argparse
import datetime
import json
import os
import sys
from pathlib import Path
from typing import Any

import google.auth.transport.requests
import requests
from dotenv import load_dotenv
from google.oauth2 import service_account

load_dotenv(Path(__file__).parent / ".env")

FIRESTORE_PROJECT_ID = os.environ["FIRESTORE_PROJECT_ID"]
_RAW_CREDENTIALS_FILE = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(Path(__file__).parent / "service_account.json"),
)
CREDENTIALS_FILE = str(
    (Path(__file__).parent / _RAW_CREDENTIALS_FILE).resolve()
    if not Path(_RAW_CREDENTIALS_FILE).is_absolute()
    else Path(_RAW_CREDENTIALS_FILE)
)
FIRESTORE_BASE = (
    f"https://firestore.googleapis.com/v1/projects/{FIRESTORE_PROJECT_ID}"
    f"/databases/(default)/documents"
)
COLLECTION = "use_cases"

# Conservative allowlist: fields already written/handled by local ingest plus seqId lookup support.
ALLOWED_PATCH_FIELDS = {
    "category",
    "sourceUser",
    "description",
    "refUrls",
    "tweetDate",
    "searchDate",
    "notes",
    "uncertainty",
    "novelty",
    "title",
    "sourceUrl",
    "subcategory",
    "confidence",
    "tags",
    "increaseScope",
}
INCREASE_SCOPE_LABELS = {
    "News",
    "Tutorials",
    "Launches",
    "Analysis",
    "Ecosystem",
    "Duplicates",
    "Blueprints",
    "Curated",
}
TYPE_CHOICES = {"string", "int", "float", "bool", "null", "json"}


def get_token() -> str:
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/datastore"],
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def auth_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def parse_firestore_value(value: dict[str, Any]) -> Any:
    if "stringValue" in value:
        return value["stringValue"]
    if "integerValue" in value:
        return int(value["integerValue"])
    if "doubleValue" in value:
        return float(value["doubleValue"])
    if "booleanValue" in value:
        return value["booleanValue"]
    if "timestampValue" in value:
        return value["timestampValue"]
    if "nullValue" in value:
        return None
    if "arrayValue" in value:
        return [parse_firestore_value(v) for v in value["arrayValue"].get("values", [])]
    if "mapValue" in value:
        return {
            k: parse_firestore_value(v)
            for k, v in value["mapValue"].get("fields", {}).items()
        }
    raise ValueError(f"Unsupported Firestore value shape: {value}")


def encode_firestore_value(value: Any) -> dict[str, Any]:
    if value is None:
        return {"nullValue": None}
    if isinstance(value, bool):
        return {"booleanValue": value}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"integerValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, str):
        return {"stringValue": value}
    if isinstance(value, list):
        return {"arrayValue": {"values": [encode_firestore_value(v) for v in value]}}
    if isinstance(value, dict):
        return {"mapValue": {"fields": {k: encode_firestore_value(v) for k, v in value.items()}}}
    raise TypeError(f"Unsupported Python value type: {type(value).__name__}")


def run_query(token: str, structured_query: dict[str, Any]) -> list[dict[str, Any]]:
    resp = requests.post(
        f"{FIRESTORE_BASE}:runQuery",
        headers=auth_headers(token),
        json={"structuredQuery": structured_query},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def fetch_collection(token: str, collection: str) -> list[dict[str, Any]]:
    url = f"{FIRESTORE_BASE}/{collection}"
    docs: list[dict[str, Any]] = []
    page_token: str | None = None
    while True:
        params: dict[str, Any] = {"pageSize": 300}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, headers=auth_headers(token), params=params, timeout=60)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        payload = resp.json()
        docs.extend(payload.get("documents", []))
        page_token = payload.get("nextPageToken")
        if not page_token:
            break
    return docs


def lookup_doc_by_seq_id(token: str, seq_id: int) -> dict[str, Any]:
    docs = fetch_collection(token, COLLECTION)
    snapshots = [extract_doc_snapshot(doc) for doc in docs]
    ordered = sorted(
        snapshots,
        key=lambda r: (
            str(r.get("tweetDate") or ""),
            str(r.get("createdAt") or ""),
            str(r.get("_id") or ""),
        ),
    )
    if seq_id < 1 or seq_id > len(ordered):
        raise SystemExit(f"seqId={seq_id} is out of range 1..{len(ordered)}")
    target = ordered[seq_id - 1]
    return get_doc_by_id(token, target["_id"])


def get_doc_by_id(token: str, doc_id: str) -> dict[str, Any]:
    resp = requests.get(
        f"{FIRESTORE_BASE}/{COLLECTION}/{doc_id}",
        headers=auth_headers(token),
        timeout=30,
    )
    if resp.status_code == 404:
        raise SystemExit(f"No use_cases record found for doc id {doc_id}")
    resp.raise_for_status()
    return resp.json()


def extract_doc_snapshot(doc: dict[str, Any]) -> dict[str, Any]:
    snapshot = {"_id": doc["name"].split("/")[-1]}
    for key, value in doc.get("fields", {}).items():
        snapshot[key] = parse_firestore_value(value)
    return snapshot


def coerce_cli_value(args: argparse.Namespace) -> Any:
    if args.json_value is not None:
        try:
            return json.loads(args.json_value)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid --json-value: {exc}") from exc

    type_name = args.type
    raw = args.value

    if type_name == "null":
        if raw is not None:
            raise SystemExit("--type null does not accept --value")
        return None
    if raw is None:
        raise SystemExit("--value is required unless --json-value or --type null is used")
    if type_name == "string":
        return raw
    if type_name == "int":
        try:
            return int(raw)
        except ValueError as exc:
            raise SystemExit(f"Invalid int value: {raw}") from exc
    if type_name == "float":
        try:
            return float(raw)
        except ValueError as exc:
            raise SystemExit(f"Invalid float value: {raw}") from exc
    if type_name == "bool":
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "y"}:
            return True
        if lowered in {"0", "false", "no", "n"}:
            return False
        raise SystemExit(f"Invalid bool value: {raw}")
    if type_name == "json":
        try:
            return json.loads(raw)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSON in --value: {exc}") from exc

    raise SystemExit(f"Unsupported type: {type_name}")


def validate_field_value(field: str, value: Any) -> None:
    if field not in ALLOWED_PATCH_FIELDS:
        raise SystemExit(
            f"Field '{field}' is not allowed. Allowed fields: {sorted(ALLOWED_PATCH_FIELDS)}"
        )
    if field == "increaseScope" and value not in INCREASE_SCOPE_LABELS:
        raise SystemExit(
            f"Invalid increaseScope '{value}'. Valid labels: {sorted(INCREASE_SCOPE_LABELS)}"
        )


def patch_field(token: str, doc_id: str, field: str, value: Any) -> dict[str, Any]:
    now = datetime.datetime.utcnow().isoformat() + "Z"
    resp = requests.patch(
        f"{FIRESTORE_BASE}/{COLLECTION}/{doc_id}",
        headers=auth_headers(token),
        params={"updateMask.fieldPaths": [field, "updatedAt"]},
        json={
            "fields": {
                field: encode_firestore_value(value),
                "updatedAt": {"timestampValue": now},
            }
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--seq-id", type=int, help="Target use_cases seqId")
    target.add_argument("--doc-id", help="Target use_cases Firestore document id")
    parser.add_argument("--field", required=True, help="Field name to patch")
    parser.add_argument("--value", help="Scalar value (default type: string)")
    parser.add_argument(
        "--type",
        default="string",
        choices=sorted(TYPE_CHOICES),
        help="Interpretation for --value (ignored when --json-value is used)",
    )
    parser.add_argument(
        "--json-value",
        help="Raw JSON value for arrays/maps/scalars, e.g. '[\"a\",\"b\"]' or 'null'",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Resolve target and print the planned patch without writing",
    )
    return parser


def main() -> None:
    args = build_parser().parse_args()
    value = coerce_cli_value(args)
    validate_field_value(args.field, value)

    token = get_token()
    doc = lookup_doc_by_seq_id(token, args.seq_id) if args.seq_id is not None else get_doc_by_id(token, args.doc_id)
    snapshot = extract_doc_snapshot(doc)
    doc_id = snapshot["_id"]
    current_value = snapshot.get(args.field)

    plan = {
        "target": {"docId": doc_id, "seqId": snapshot.get("seqId")},
        "field": args.field,
        "current": current_value,
        "new": value,
        "dryRun": args.dry_run,
    }
    print(json.dumps(plan, indent=2, sort_keys=True, default=str))

    if args.dry_run:
        return

    updated = patch_field(token, doc_id, args.field, value)
    updated_snapshot = extract_doc_snapshot(updated)
    print(
        json.dumps(
            {
                "status": "patched",
                "target": {"docId": doc_id, "seqId": updated_snapshot.get("seqId")},
                "field": args.field,
                "value": updated_snapshot.get(args.field),
                "updatedAt": updated_snapshot.get("updatedAt"),
            },
            indent=2,
            sort_keys=True,
            default=str,
        )
    )


if __name__ == "__main__":
    try:
        main()
    except requests.HTTPError as exc:
        body = exc.response.text[:2000] if exc.response is not None else ""
        raise SystemExit(f"Firestore request failed: {exc}\n{body}") from exc
    except KeyboardInterrupt:
        raise SystemExit(130)
