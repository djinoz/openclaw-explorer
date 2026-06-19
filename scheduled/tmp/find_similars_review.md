# find_similars review — duplicate/repost handling (#1)

## Scope
This note focuses on **level 1 similars**: duplicates, reposts, and near-duplicate coverage of the same underlying story.

I am **not** recommending that the current duplicate pipeline also absorb **level 2 similars** (same class of use case, e.g. invoice processing + Xero integrations). That should be a separate categorization layer. Notes on that are at the end.

## What the current script does well
- It is explicitly tuned to avoid over-merging broad thematic similarity and instead asks the model to group only when meaningful information would not be lost by merging.
- It keeps a `leadId` plus `memberIds` structure, which is a good fit for duplicate/repost handling.
- It can extend existing groups by injecting current group leads as anchors into later batches.

## Main scaling concern
The current design is likely to **degrade in recall as the corpus grows**, even before it becomes too expensive.

The core reason is that the script does **category-bounded, fixed-size batch comparisons** and then checkpoints records as "seen". Once a record has been analyzed and left ungrouped, it is mostly frozen out of future duplicate detection.

## Evidence from current state
- Corpus size now: **938** use cases.
- Existing groups: **29** groups covering **65** records.
- Still ungrouped: **873** records.
- Checkpoint file currently contains **870** record IDs, so almost the entire ungrouped corpus has already been marked as analyzed.
- Largest ungrouped categories are already large enough to force many LLM batches:
  - Engineering: **281** records → **10** batches at size 30
  - Productivity: **244** → **9** batches
  - Marketing: **101** → **4** batches
  - Finance: **59** → **2** batches
- Across categories with at least 2 records, the current corpus implies about **39 Claude batch calls** at the current batching rule.

## Why this is suboptimal for level-1 duplicate detection

### 1) Cross-batch duplicates are easy to miss
Within a category, records are split into chunks of `MAX_RECORDS_PER_BATCH` (default 30). Records in batch A are not compared directly with records in batch B unless one of them is already an anchor from an existing group.

That means:
- two ungrouped duplicates landing in different batches are invisible to each other;
- if the first pass misses them, they do not become anchors;
- the checkpoint then marks both records as analyzed anyway.

So batching is not just a cost optimization here — it changes recall.

### 2) The checkpoint model freezes old false negatives
The checkpoint stores record IDs, not comparison state or candidate state. In practice this means:
- an old ungrouped record that was analyzed once is skipped on later runs;
- a newly ingested repost of that same story will not be compared against that old record unless the old record already belongs to an existing group lead;
- therefore the system gets worse at finding duplicates between **new records and old orphaned records** over time.

This is the biggest structural issue for #1.

### 3) Existing-group anchors help only one subset of cases
Anchor injection only uses **existing group leads** in the same category. That is useful for extending already-discovered groups, but it does nothing for:
- old records that should have been grouped but were missed earlier;
- duplicate clusters whose first valid pair was split across batches;
- transitive duplicate chains spread across multiple batches before any anchor exists.

### 4) The script leans on the LLM too early
For level-1 duplicates, a large fraction of the work should be narrowed down deterministically before Claude is asked.

Right now the script sends truncated text plus only the **first** `refUrls` URL. It does not first build cheap candidate buckets from stronger signals such as:
- normalized source URL / ref URLs
- shared canonical article URL
- same company + same product + close date
- same announcement / launch / funding / release entities
- heavy lexical overlap after normalization

This means the expensive step is doing both candidate generation and final judgment.

### 5) Category is too coarse a blocking key for duplicates
For #1, category is only a rough prior. In the current dataset, categories like Engineering and Productivity are already so broad that batching is doing a lot of accidental pruning.

For duplicates/reposts, the more natural blocking keys are things like:
- normalized URLs / domains
- extracted company or product names
- date windows
- named-entity signatures
- similarity fingerprints on normalized text

## Practical recommendation for level-1 duplicate handling

### Recommended architecture: two-stage duplicate detection

#### Stage A — cheap candidate generation (deterministic / local)
Before any LLM call, generate candidate pairs or mini-clusters using fast signals:
- **URL normalization**: parse *all* URLs from `refUrls` (not just the first), plus `sourceUrl` if present.
- **Canonicalization**: strip tracking params, normalize host/path, collapse known redirect patterns.
- **Text fingerprinting**: normalized title/description shingles, MinHash/SimHash, or TF-IDF nearest neighbors.
- **Entity blocking**: same company/product names + close dates.
- **Date windows**: duplicates/reposts usually cluster within a narrow time band.

The goal is to avoid asking Claude to scan an entire category. Claude should only review a small candidate set that already looks suspicious.

#### Stage B — LLM adjudication on candidate sets
Use Claude only to decide among candidate pairs/clusters whether they are:
- the same underlying story,
- a repost/coverage variant,
- an update that should still sit in the same duplicate family,
- or genuinely distinct stories.

That preserves the current domain-sensitive reasoning, but moves it to the right place in the pipeline.

## Minimum changes that would materially help without a full redesign
1. **Stop checkpointing by raw record ID alone.**
   - Checkpoint by comparison epoch, candidate-bucket hash, or ingestion watermark.
   - Old ungrouped records need to remain eligible for comparison against new arrivals.

2. **Compare new records against a retained candidate index of old records.**
   - Even if the full historical corpus is not re-run, every new record should be compared against likely historical matches.

3. **Use all URLs, not only the first `refUrls` entry.**
   - There is already code elsewhere in the repo (`record_urls` in `tag_increase_scope.py`) that parses all `refUrls`; the duplicate pipeline should do the same.

4. **Replace category-wide batching with candidate buckets.**
   - Batches should be formed from probable matches, not arbitrary slices of a broad category.

5. **Persist a duplicate-family index.**
   - Once a story family is established, future ingests should be matched against the family fingerprint, not only against the current lead record.

## Suggested near-term implementation path
1. Build a local candidate generator for duplicates only.
   - Inputs: normalized URLs, normalized description text, company/product/date hints.
2. For each new ingest, fetch only likely historical candidates.
3. Run Claude on those small candidate groups.
4. Store extra metadata on `use_case_groups` or a side collection:
   - canonical URLs
   - normalized entity keys
   - date span
   - text fingerprint(s)
5. Periodically run a slower backfill job to recover old missed duplicates.

## Notes for level-2 similars (future work only)
Level 2 should be treated as a **separate classification / taxonomy problem**, not as duplicate grouping.

Example: invoice-processing + Xero integration stories are usually **not duplicates**; they are a recurring use-case class.

A future approach could be:
- keep duplicate groups (#1) as one layer;
- add a separate category/subcategory/topic layer for recurring patterns (#2);
- derive topic labels from entities + workflow verbs + integration targets;
- optionally use embeddings or classifier-assisted tagging to cluster use-case classes such as:
  - invoice processing
  - accounts payable automation
  - Xero integration
  - ERP/accounting reconciliation

In other words:
- **#1 = identity / same-story resolution**
- **#2 = semantic class / taxonomy resolution**

Those should not share the same decision boundary.

## Bottom line
For duplicate/repost handling, the biggest issue is not just runtime cost. It is that the current batching + checkpoint design can permanently miss duplicates once the corpus is large enough that related records stop landing in the same batch.

If this is meant to stay reliable as OpenClaw grows, `find_similars` should be refactored into:
- deterministic candidate generation first,
- LLM adjudication second,
- and incremental comparison of new records against historical candidate indexes rather than one-shot category batches.
