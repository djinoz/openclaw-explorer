#!/usr/bin/env python3
"""
OpenClaw similarity finder
==========================
Uses Claude (domain-aware reasoning) to find semantically similar or duplicate
use cases in the Firestore database and proposes groupings in the
use_case_groups collection for owner review.

Domain-aware: treats similar-sounding use cases differently when they belong to
distinct specific domains. "YouTube video pipeline" is NOT the same as "tweet
production pipeline" even though both are content-creation workflows.

Usage:
  python3 find_similars.py              # propose groups and write to Firestore
  DRY_RUN=1 python3 find_similars.py   # print proposals only, no writes

Environment variables (in .env):
  GOOGLE_APPLICATION_CREDENTIALS   path to service_account.json
  FIRESTORE_PROJECT_ID              Firebase project ID
  ANTHROPIC_API_KEY                 Claude API key
  DRY_RUN                           if "1", print groups but don't write
  MIN_CLUSTER_SIZE                  min records per category to analyse (default 2)
  MAX_RECORDS_PER_BATCH             max records sent to Claude at once (default 30)
"""

from __future__ import annotations

import json
import logging
import os
import datetime
from pathlib import Path

import requests
from anthropic import Anthropic
from dotenv import load_dotenv
from google.oauth2 import service_account
import google.auth.transport.requests

load_dotenv(Path(__file__).parent / ".env")

FIRESTORE_PROJECT_ID = os.environ["FIRESTORE_PROJECT_ID"]
ANTHROPIC_API_KEY    = os.environ["ANTHROPIC_API_KEY"]
DRY_RUN              = os.environ.get("DRY_RUN", "0") == "1"
DEBUG_CLAUDE         = os.environ.get("DEBUG_CLAUDE", "0") == "1"
CREDENTIALS_FILE     = os.environ.get(
    "GOOGLE_APPLICATION_CREDENTIALS",
    str(Path(__file__).parent / "service_account.json"),
)
MIN_CLUSTER_SIZE      = int(os.environ.get("MIN_CLUSTER_SIZE", "2"))
MAX_RECORDS_PER_BATCH = int(os.environ.get("MAX_RECORDS_PER_BATCH", "30"))
CHECKPOINT_FILE       = Path(os.environ.get(
    "SIMILARITY_CHECKPOINT",
    str(Path(__file__).parent / "similarity_checkpoint.json"),
))

FIRESTORE_BASE = (
    f"https://firestore.googleapis.com/v1/projects/{FIRESTORE_PROJECT_ID}"
    f"/databases/(default)/documents"
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

# ── Claude prompt ──────────────────────────────────────────────────────────────

SIMILARITY_SYSTEM = """\
You are a research analyst identifying duplicate or near-duplicate entries in a
use-case database.

GROUP records when any of these apply:
  • The same original announcement, tweet, or product launch reported from
    multiple sources with minor wording differences.
  • The same company's identical deployment described in separate records
    (e.g. one from the company's blog, one from a news article, one from
    a tweet — all describing the same rollout).
  • Descriptions that are paraphrases of each other with no meaningful
    informational difference.
  • One record is an update or follow-up to the same use case.

DO NOT group records that merely share a broad domain or theme.
These are DIFFERENT use cases even when superficially similar:
  • Same workflow type but different platforms/companies/languages.
  • Same industry but different regulatory or business contexts.
  • Same product category but distinct product names.

Practical test: would a researcher merging these records lose meaningful
information? If yes — keep separate. If no — group them.

For each proposed group, identify the "lead" record: the most detailed,
earliest-dated, or most authoritative source. All others are "members".

Respond ONLY with a JSON array. Each group must be:
  {"leadSeqId": <number>, "memberSeqIds": [<number>, ...], "reason": "<one sentence>"}

If nothing qualifies, respond with exactly: []
"""


# ── Firestore helpers ──────────────────────────────────────────────────────────

def get_token() -> str:
    creds = service_account.Credentials.from_service_account_file(
        CREDENTIALS_FILE,
        scopes=["https://www.googleapis.com/auth/datastore"],
    )
    creds.refresh(google.auth.transport.requests.Request())
    return creds.token


def auth_headers(token: str) -> dict:
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _parse_value(v: dict):
    if "stringValue"  in v: return v["stringValue"]
    if "integerValue" in v: return int(v["integerValue"])
    if "doubleValue"  in v: return float(v["doubleValue"])
    if "booleanValue" in v: return v["booleanValue"]
    if "timestampValue" in v: return v["timestampValue"]
    if "nullValue"    in v: return None
    if "arrayValue"   in v:
        return [_parse_value(i) for i in v["arrayValue"].get("values", [])]
    return None


def parse_doc(doc: dict) -> dict:
    result = {"_id": doc["name"].split("/")[-1]}
    for k, v in doc.get("fields", {}).items():
        result[k] = _parse_value(v)
    return result


def fetch_collection(token: str, collection: str) -> list[dict]:
    url = f"{FIRESTORE_BASE}/{collection}"
    all_docs, page_token = [], None
    while True:
        params: dict = {"pageSize": 300}
        if page_token:
            params["pageToken"] = page_token
        resp = requests.get(url, headers=auth_headers(token), params=params, timeout=30)
        if resp.status_code == 404:
            return []
        resp.raise_for_status()
        data = resp.json()
        all_docs.extend(data.get("documents", []))
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return [parse_doc(d) for d in all_docs]


def write_group(token: str, lead_id: str, member_ids: list[str], reason: str) -> str:
    now = datetime.datetime.utcnow().isoformat() + "Z"
    fields = {
        "leadId":    {"stringValue": lead_id},
        "memberIds": {"arrayValue": {"values": [{"stringValue": m} for m in member_ids]}},
        "reason":    {"stringValue": reason},
        "source":    {"stringValue": "cli"},
        "status":    {"stringValue": "pending"},
        "createdAt": {"timestampValue": now},
        "updatedAt": {"timestampValue": now},
    }
    resp = requests.post(
        f"{FIRESTORE_BASE}/use_case_groups",
        headers=auth_headers(token),
        json={"fields": fields},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json().get("name", "").split("/")[-1]


def extend_group(token: str, group: dict, new_member_ids: list[str]) -> None:
    existing = list(group.get("memberIds") or [])
    merged = list(dict.fromkeys(existing + new_member_ids))  # deduplicate, preserve order
    if merged == existing:
        return
    now = datetime.datetime.utcnow().isoformat() + "Z"
    gid = group["_id"]
    resp = requests.patch(
        f"{FIRESTORE_BASE}/use_case_groups/{gid}",
        headers=auth_headers(token),
        params={"updateMask.fieldPaths": ["memberIds", "updatedAt"]},
        json={"fields": {
            "memberIds": {"arrayValue": {"values": [{"stringValue": m} for m in merged]}},
            "updatedAt": {"timestampValue": now},
        }},
        timeout=30,
    )
    resp.raise_for_status()


# ── Seq-ID assignment (matches app client-side ordering) ──────────────────────

def assign_seq_ids(records: list[dict]) -> list[dict]:
    sorted_recs = sorted(records, key=lambda r: (r.get("tweetDate", ""), r.get("createdAt", "")))
    for i, r in enumerate(sorted_recs):
        r["seqId"] = i + 1
    return sorted_recs


# ── Claude similarity analysis ────────────────────────────────────────────────

def format_for_claude(records: list[dict]) -> str:
    lines = []
    for r in records:
        first_url = (r.get("refUrls") or "").split(",")[0].strip()
        notes_snippet = (r.get("notes") or "")[:120]
        lines.append(
            f"#{r['seqId']} [{r.get('category', '')}] {r.get('sourceUser', '')} | {r.get('tweetDate', '')}\n"
            f"  DESC: {r.get('description', '')[:220]}\n"
            f"  NOTES: {notes_snippet}\n"
            f"  URL: {first_url}"
        )
    return "\n\n".join(lines)


def merge_proposals(proposals: list[dict]) -> list[dict]:
    """Merge proposals that share any record ID into a single larger group."""
    # Build union-find over seqIds
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent.get(x, x), x)  # path compression
            x = parent.get(x, x)
        return x

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    # Record all seqIds involved and union them within each proposal
    all_ids: set[int] = set()
    for p in proposals:
        lead = p.get("leadSeqId")
        members = p.get("memberSeqIds") or []
        if lead is None:
            continue
        all_ids.add(lead)
        for m in members:
            all_ids.add(m)
            union(lead, m)

    # Group seqIds by their root
    groups: dict[int, set[int]] = {}
    for sid in all_ids:
        root = find(sid)
        groups.setdefault(root, set()).add(sid)

    # Build merged proposals: lead = lowest seqId in group (most likely the earliest)
    # Also collect reasons from original proposals
    reasons: dict[int, list[str]] = {}  # root → reasons
    lead_votes: dict[int, dict[int, int]] = {}  # root → {seqId: vote_count}
    for p in proposals:
        lead = p.get("leadSeqId")
        if lead is None:
            continue
        root = find(lead)
        reasons.setdefault(root, [])
        if p.get("reason"):
            reasons[root].append(p["reason"])
        lead_votes.setdefault(root, {})
        lead_votes[root][lead] = lead_votes[root].get(lead, 0) + 1

    merged = []
    for root, sids in groups.items():
        if len(sids) < 2:
            continue
        # Pick lead: most-voted, then lowest seqId as tiebreak
        vote_map = lead_votes.get(root, {})
        lead_seq = min(sids, key=lambda s: (-vote_map.get(s, 0), s))
        member_seqs = sorted(sids - {lead_seq})
        reason = "; ".join(dict.fromkeys(reasons.get(root, [])))  # deduplicate
        merged.append({"leadSeqId": lead_seq, "memberSeqIds": member_seqs, "reason": reason})

    return merged


def find_similars_in_cluster(client: Anthropic, records: list[dict]) -> list[dict]:
    records_text = format_for_claude(records)
    log.debug("Sending %d records to Claude", len(records))
    resp = client.messages.create(
        model="claude-sonnet-4-6",
        max_tokens=2048,
        system=SIMILARITY_SYSTEM,
        messages=[{
            "role": "user",
            "content": (
                f"Identify duplicate or near-duplicate use cases in this cluster "
                f"({len(records)} records). Return only a JSON array.\n\n"
                f"{records_text}"
            ),
        }],
    )
    raw = resp.content[0].text.strip()
    if DEBUG_CLAUDE:
        log.info("Claude raw response:\n%s", raw[:2000])
    # Strip markdown fences if Claude wraps the JSON
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    # Extract the JSON array even if Claude prepends reasoning text
    start = raw.find("[")
    end   = raw.rfind("]")
    if start != -1 and end != -1 and end > start:
        raw = raw[start : end + 1]
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Could not parse Claude response: %s", raw[:300])
        return []


# ── Checkpoint (skip already-analysed batches) ────────────────────────────────

def load_checkpoint() -> set[str]:
    if CHECKPOINT_FILE.exists():
        return set(json.loads(CHECKPOINT_FILE.read_text()))
    return set()


def save_checkpoint(seen: set[str]) -> None:
    if not DRY_RUN:
        CHECKPOINT_FILE.write_text(json.dumps(sorted(seen)))


# ── Main ───────────────────────────────────────────────────────────────────────

def main():
    token  = get_token()
    client = Anthropic(api_key=ANTHROPIC_API_KEY)

    seen = load_checkpoint()
    log.info("Checkpoint: %d previously-analysed record IDs", len(seen))

    log.info("Fetching use_cases…")
    records = fetch_collection(token, "use_cases")
    records = assign_seq_ids(records)
    log.info("Loaded %d records", len(records))

    log.info("Fetching existing use_case_groups…")
    existing_groups = fetch_collection(token, "use_case_groups")
    # Build set of doc IDs already in a non-rejected group
    already_grouped: set[str] = set()
    for g in existing_groups:
        if g.get("status") != "rejected":
            already_grouped.add(g.get("leadId", ""))
            for mid in (g.get("memberIds") or []):
                already_grouped.add(mid)
    log.info(
        "Found %d existing groups covering %d records",
        len(existing_groups), len(already_grouped),
    )

    ungrouped = [r for r in records if r["_id"] not in already_grouped]
    log.info("%d records not yet in any group", len(ungrouped))

    # Map seqId → Firestore doc ID for resolving Claude's output
    seq_to_doc: dict[int, str] = {r["seqId"]: r["_id"] for r in records}
    doc_to_record: dict[str, dict] = {r["_id"]: r for r in records}

    # Map lead doc ID → existing group (for extending groups with new members)
    lead_doc_to_group: dict[str, dict] = {}
    for g in existing_groups:
        if g.get("status") != "rejected" and g.get("leadId"):
            lead_doc_to_group[g["leadId"]] = g

    # Build per-category anchor records (leads of existing groups in same category)
    # so ungrouped records can be matched against them
    anchor_clusters: dict[str, list[dict]] = {}
    for lead_doc_id, group in lead_doc_to_group.items():
        rec = doc_to_record.get(lead_doc_id)
        if not rec:
            continue
        key = rec.get("category") or "(unknown)"
        if rec.get("subcategory"):
            key = f"{key} / {rec['subcategory']}"
        anchor_rec = dict(rec)
        anchor_rec["_anchor"] = True
        anchor_clusters.setdefault(key, []).append(anchor_rec)

    # Cluster by category (and optionally subcategory)
    clusters: dict[str, list[dict]] = {}
    for r in ungrouped:
        key = r.get("category") or "(unknown)"
        if r.get("subcategory"):
            key = f"{key} / {r['subcategory']}"
        clusters.setdefault(key, []).append(r)

    total_proposed = 0

    for cat, cluster in sorted(clusters.items(), key=lambda x: -len(x[1])):
        if len(cluster) < MIN_CLUSTER_SIZE:
            log.info("  SKIP %r (%d record, below MIN_CLUSTER_SIZE=%d)",
                     cat, len(cluster), MIN_CLUSTER_SIZE)
            continue

        log.info("  Analysing %r (%d records)…", cat, len(cluster))

        # Split into batches if needed
        batches = [
            cluster[i : i + MAX_RECORDS_PER_BATCH]
            for i in range(0, len(cluster), MAX_RECORDS_PER_BATCH)
        ]
        if len(batches) > 1:
            log.info("    → split into %d batches of ≤%d", len(batches), MAX_RECORDS_PER_BATCH)

        anchors = anchor_clusters.get(cat, [])

        for batch_idx, batch in enumerate(batches):
            batch_ids = {r["_id"] for r in batch}
            if batch_ids.issubset(seen):
                log.info("    batch %d: all %d records already analysed, skipping",
                         batch_idx + 1, len(batch))
                continue

            new_count = len(batch_ids - seen)
            log.info("    batch %d: %d new record(s) in batch", batch_idx + 1, new_count)

            # Include anchor records (existing group leads) so orphans can match them.
            # Anchors are appended after the ungrouped batch records.
            batch_with_anchors = batch + [a for a in anchors if a["_id"] not in batch_ids]
            if len(batch_with_anchors) > len(batch):
                log.info("    batch %d: +%d anchor(s) injected",
                         batch_idx + 1, len(batch_with_anchors) - len(batch))

            proposals = find_similars_in_cluster(client, batch_with_anchors)
            proposals = merge_proposals(proposals)
            seen.update(batch_ids)
            save_checkpoint(seen)

            if not proposals:
                log.info("    batch %d: no groups proposed", batch_idx + 1)
                continue

            log.info("    batch %d: %d group(s) proposed", batch_idx + 1, len(proposals))

            for proposal in proposals:
                lead_seq    = proposal.get("leadSeqId")
                member_seqs = proposal.get("memberSeqIds") or []
                reason      = proposal.get("reason", "")

                if not lead_seq or not member_seqs:
                    log.warning("    Skipping malformed proposal: %s", proposal)
                    continue

                lead_doc_id    = seq_to_doc.get(lead_seq)
                member_doc_ids = [seq_to_doc[s] for s in member_seqs if s in seq_to_doc]

                if not lead_doc_id or not member_doc_ids:
                    log.warning(
                        "    Could not resolve seqIds for proposal (lead=%s members=%s)",
                        lead_seq, member_seqs,
                    )
                    continue

                # Filter out member IDs that are already in ANY existing group
                # (anchors are already grouped; only add truly ungrouped members)
                new_member_ids = [m for m in member_doc_ids if m not in already_grouped]

                existing_group = lead_doc_to_group.get(lead_doc_id)

                if DRY_RUN:
                    action = "EXTEND" if existing_group else "NEW GROUP"
                    print(json.dumps({
                        "action":        action,
                        "leadSeqId":     lead_seq,
                        "memberSeqIds":  member_seqs,
                        "reason":        reason,
                        "leadDocId":     lead_doc_id,
                        "newMemberDocIds": new_member_ids,
                    }, indent=2))
                elif existing_group:
                    if not new_member_ids:
                        log.info("    batch %d: anchor #%s matched but no new members to add",
                                 batch_idx + 1, lead_seq)
                        continue
                    extend_group(token, existing_group, new_member_ids)
                    log.info(
                        "    EXTENDED GROUP %s ← +%s | %s",
                        existing_group["_id"], new_member_ids, reason[:70],
                    )
                    total_proposed += 1
                else:
                    if not new_member_ids:
                        log.info("    batch %d: proposal #%s has no ungrouped members, skipping",
                                 batch_idx + 1, lead_seq)
                        continue
                    gid = write_group(token, lead_doc_id, new_member_ids, reason)
                    log.info(
                        "    NEW GROUP #%s ← %s | %s [%s]",
                        lead_seq, member_seqs, reason[:70], gid,
                    )
                    total_proposed += 1

    if DRY_RUN:
        log.info("DRY RUN — nothing written to Firestore")
    else:
        log.info("Done. Proposed %d new group(s).", total_proposed)


if __name__ == "__main__":
    main()
