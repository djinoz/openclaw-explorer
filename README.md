# OpenClaw Explorer

A React + Firebase web app for browsing, searching, and triaging Claude AI use cases collected from social media and the web. Use cases are collected daily by an automated scheduled task and stored in Firestore.

Security notes:

- Public reads are intended; writes to converted entries are restricted to the verified owner email in Firestore rules.
- Service account files and API keys stay local or in Firebase secrets; they are never committed.
- The browser only receives public data and never needs Firestore admin credentials.

## Features

- Real-time Firestore sync
- Google Sign-In for contributor attribution and owner-only editing
- Public suggestion queue for URL submissions
- Sort and filter by category, date, confidence, novelty
- Full-text client-side search
- Detail panel with full free-text notes
- Inline editing
- Triage panel (send a batch of records to Claude for deduplication, reclassification, etc.)
- CSV import / export
- Stats modal

## Future work

- **Tags**: Attach project-domain tags (e.g. `#healthcare`, `#coding`, `#legal`) to each record for richer filtering. The data model supports an optional `tags` array field — the UI and Firestore indexes just need to be extended.

---

## Prerequisites

- Node.js 18+
- Firebase CLI: `npm install -g firebase-tools`
- A Firebase project with Firestore and Authentication enabled

---

## Firebase project setup

1. Go to [console.firebase.google.com](https://console.firebase.google.com) and create a project named `openclaw-explorer` (or any name you prefer).

2. Enable **Cloud Firestore** in Native mode.

3. Enable **Authentication → Sign-in method → Google**.

4. Register a **Web app** inside the project and copy the config values.

5. In **Firestore → Rules**, deploy the rules in `firestore.rules` (or let `firebase deploy` handle it).

6. Deploy the Cloud Functions in `functions/` so the suggestion queue can issue anonymous sessions and accept submissions.

---

## Local development

```bash
# 1. Clone
git clone https://github.com/djinoz/openclaw-explorer.git
cd openclaw-explorer

# 2. Install
npm install

# 3. Configure environment
cp .env.example .env.local
# Edit .env.local and fill in your Firebase web config values only

# 4. Run dev server
npm run dev
```

If you want to exercise the suggestion queue end-to-end locally, run the Firebase emulators for Firestore and Functions in a second terminal.

---

## Deployment

```bash
# Build and deploy to Firebase Hosting in one step
npm run deploy
```

On first run you will be prompted to log in with `firebase login`.

The `firebase.json` config points to `dist/` and sets up SPA rewrites so all routes resolve to `index.html`.

---

## Seeding Firestore with sample data

A seed script is included in `scripts/seed-firestore.js`. It uses the Firebase Admin SDK and requires a service account key stored locally.

```bash
# 1. In Firebase Console → Project settings → Service accounts → Generate new private key
#    Save the downloaded file as `scripts/service_account.json`
#    (this file is in `.gitignore` — never commit it)

# 2. Run the seed script
node scripts/seed-firestore.js
```

The script is idempotent: it checks for existing records by `sourceUrl` before inserting.

---

## Scheduled ingest task

The daily use-case ingest lives in `scheduled/ingest.py`. It now expects JSON records on `stdin` from your scheduler or upstream extraction step, then:

1. Validates and normalizes the incoming records
2. Deduplicates against existing Firestore records
3. Writes new records via the Firestore REST API using a service account

If you want Firebase to run the extraction itself, see `functions/main.py` for the Cloud Scheduler version. That path keeps Anthropic and search keys in Firebase secrets instead of code.

### Setup

```bash
cd scheduled
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy your service account key to `scheduled/service_account.json` (git-ignored) and keep it off shared machines.

Set these environment variables (e.g. in a `.env` file that is also git-ignored):

| Variable | Description |
|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to `service_account.json` |
| `FIRESTORE_PROJECT_ID` | Your Firebase project ID |
| `ANTHROPIC_API_KEY` | Claude API key, if your upstream scheduler does extraction |
| `SEARCH_API_KEY` | Web search API key, if your upstream scheduler does search |

### Shared scheduled runtime + MCP query server

The `scheduled/` folder is now a **single shared runtime** for both:

- the existing scheduled ingest/similarity jobs
- the local read-only Firestore MCP server

That means both flows should use the same:
- virtualenv: `scheduled/.venv`
- env file: `scheduled/.env`
- credentials file: `scheduled/service_account.json` (or another path via `GOOGLE_APPLICATION_CREDENTIALS`)

Key shared env vars:
- `FIRESTORE_PROJECT_ID`
- `GOOGLE_APPLICATION_CREDENTIALS`
- `ANTHROPIC_API_KEY` (needed for similarity/grouping jobs)
- `SEARCH_API_KEY` (if your upstream scheduled collection flow uses it)
- `DRY_RUN`

Setup:

```bash
cd scheduled
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in FIRESTORE_PROJECT_ID and local credentials
# add ANTHROPIC_API_KEY / SEARCH_API_KEY if your scheduled jobs need them
python test_openclaw_db_mcp.py
```

Legacy scheduled jobs still run from this same environment, for example:

```bash
cd scheduled
source .venv/bin/activate
cat pending_records_YYYY-MM-DD_HH.json | python ingest.py
python find_similars.py
```

The MCP server files are:
- server file: `scheduled/mcp_server.py`
- shared Firestore client logic: `scheduled/firestore_client.py`
- smoke test: `scheduled/test_openclaw_db_mcp.py`

If you want to register it with OpenClaw MCP, configure a stdio server that runs the scheduled venv Python with `mcp_server.py` as the sole argument. Do not commit machine-specific absolute paths or local config exports.

The MCP server exposes:
- `search_use_cases`
- `get_use_case`
- `get_stats`
- `list_categories`
- `get_groups`
- `get_suggestion_queue`
- `refresh_cache`

### Running manually

```bash
echo '[{"description":"demo","refUrls":"https://example.com"}]' | python3 ingest.py
```

### Scheduling (macOS launchd)

A sample plist is at `scheduled/com.openclaw.ingest.plist`. Edit the paths and environment variables, then:

```bash
cp scheduled/com.openclaw.ingest.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.openclaw.ingest.plist
```

The plist is configured to run daily at 08:00 and should only reference local files and local secrets.

### Scheduling (Linux cron)

```cron
0 8 * * * cd /path/to/openclaw-explorer/scheduled && .venv/bin/python3 ingest.py >> logs/ingest.log 2>&1
```

---

## Firestore data model

Collection: `use_cases`

| Field | Type | Description |
|---|---|---|
| `title` | string | Short descriptive title |
| `category` | string | e.g. "Coding", "Writing", "Research" |
| `subcategory` | string | Optional subcategory |
| `description` | string | One-sentence summary |
| `notes` | string | Free-text notes, links, quotes (multiline) |
| `refUrls` | string | Comma-separated source URLs |
| `sourceUrl` | string | Legacy single-URL field used by seed/import scripts |
| `tweetDate` | string | Date of source (YYYY-MM-DD) |
| `confidence` | number | 0–10 confidence score |
| `novelty` | number | 0–10 novelty score |
| `createdAt` | timestamp | Firestore server timestamp |
| `updatedAt` | timestamp | Firestore server timestamp |
| `tags` | array\<string\> | (future) domain tags |

Sequential display IDs (`seqId`) are assigned client-side by sorting on `tweetDate` + `createdAt` — they are not stored in Firestore.
