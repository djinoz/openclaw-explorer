# OpenClaw Explorer

A React + Firebase web app for browsing, searching, and triaging Claude AI use cases collected from social media and the web. Use cases are collected daily by an automated scheduled task and stored in Firestore.

## Features

- Real-time Firestore sync
- Google Sign-In (required to add/edit/delete records)
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
# Edit .env.local and fill in your Firebase config values

# 4. Run dev server
npm run dev
```

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

A seed script is included in `scripts/seed-firestore.js`. It uses the Firebase Admin SDK and requires a service account key.

```bash
# 1. In Firebase Console → Project settings → Service accounts → Generate new private key
#    Save the downloaded file as scripts/service_account.json
#    (this file is in .gitignore — never commit it)

# 2. Run the seed script
node scripts/seed-firestore.js
```

The script is idempotent: it checks for existing records by `sourceUrl` before inserting.

---

## Scheduled ingest task

The daily use-case ingest lives in `scheduled/ingest.py`. It:

1. Calls a web search / Claude API to discover new Claude use cases
2. Deduplicates against existing Firestore records
3. Writes new records via the Firestore REST API using a service account

### Setup

```bash
cd scheduled
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Copy your service account key to `scheduled/service_account.json` (git-ignored).

Set these environment variables (e.g. in a `.env` file that is also git-ignored):

| Variable | Description |
|---|---|
| `GOOGLE_APPLICATION_CREDENTIALS` | Path to `service_account.json` |
| `FIRESTORE_PROJECT_ID` | Your Firebase project ID |
| `ANTHROPIC_API_KEY` | Claude API key for use-case extraction |
| `SEARCH_API_KEY` | Web search API key (e.g. Tavily or Serper) |

### Running manually

```bash
python3 ingest.py
```

### Scheduling (macOS launchd)

A sample plist is at `scheduled/com.openclaw.ingest.plist`. Edit the paths and environment variables, then:

```bash
cp scheduled/com.openclaw.ingest.plist ~/Library/LaunchAgents/
launchctl load ~/Library/LaunchAgents/com.openclaw.ingest.plist
```

The plist is configured to run daily at 08:00.

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
| `sourceUrl` | string | Original tweet / article URL |
| `tweetDate` | string | Date of source (YYYY-MM-DD) |
| `confidence` | number | 0–10 confidence score |
| `novelty` | number | 0–10 novelty score |
| `createdAt` | timestamp | Firestore server timestamp |
| `updatedAt` | timestamp | Firestore server timestamp |
| `tags` | array\<string\> | (future) domain tags |

Sequential display IDs (`seqId`) are assigned client-side by sorting on `tweetDate` + `createdAt` — they are not stored in Firestore.
