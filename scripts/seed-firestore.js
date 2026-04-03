#!/usr/bin/env node
/**
 * Seed Firestore with sample use cases.
 *
 * Prerequisites:
 *   - Place your Firebase service account JSON at scripts/service_account.json
 *   - npm install (firebase-admin is a devDependency)
 *
 * Usage:
 *   node scripts/seed-firestore.js
 */

import admin from 'firebase-admin'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, join } from 'path'

const __dirname = dirname(fileURLToPath(import.meta.url))

const serviceAccount = JSON.parse(
  readFileSync(join(__dirname, 'service_account.json'), 'utf8')
)

admin.initializeApp({
  credential: admin.credential.cert(serviceAccount),
})

const db = admin.firestore()

// ---------------------------------------------------------------------------
// Sample seed records
// Add your own or import from CSV via the app's Import feature.
// ---------------------------------------------------------------------------
const SEED = [
  {
    title: 'Automated contract review',
    category: 'Legal',
    subcategory: 'Contract analysis',
    description: 'Law firm uses Claude to review NDAs and flag unusual clauses.',
    notes: 'Reported 70% reduction in junior associate time on first-pass review.\nSource: @legaltech_founder tweet thread.',
    sourceUrl: 'https://twitter.com/legaltech_founder/status/example1',
    tweetDate: '2024-11-15',
    confidence: 9,
    novelty: 7,
  },
  {
    title: 'Patient discharge summary generation',
    category: 'Healthcare',
    subcategory: 'Clinical documentation',
    description: 'Hospital system drafts discharge summaries from structured EHR data.',
    notes: 'Pilot at 3 hospitals. Clinician edits average < 2 minutes per summary.',
    sourceUrl: 'https://twitter.com/health_ai_news/status/example2',
    tweetDate: '2024-11-20',
    confidence: 8,
    novelty: 8,
  },
  {
    title: 'Code review assistant',
    category: 'Coding',
    subcategory: 'Code review',
    description: 'Engineering team runs Claude on every PR to catch logic bugs and style issues.',
    notes: 'Integrated via GitHub Actions. Avg 12 comments per PR, ~40% accepted by engineers.',
    sourceUrl: 'https://twitter.com/devtools_demo/status/example3',
    tweetDate: '2024-12-01',
    confidence: 9,
    novelty: 5,
  },
  {
    title: 'Earnings call transcript analysis',
    category: 'Finance',
    subcategory: 'Investment research',
    description: 'Hedge fund extracts management sentiment signals from earnings transcripts at scale.',
    notes: 'Processes 500+ transcripts per quarter. Backtests show modest alpha on sentiment signal.',
    sourceUrl: 'https://twitter.com/quant_trader/status/example4',
    tweetDate: '2024-12-05',
    confidence: 7,
    novelty: 8,
  },
  {
    title: 'Customer support ticket routing',
    category: 'Customer Support',
    subcategory: 'Triage',
    description: 'SaaS company uses Claude to classify and route inbound support tickets.',
    notes: 'Accuracy 94% vs 87% previous ML classifier. Handles 2000 tickets/day.',
    sourceUrl: 'https://twitter.com/saas_ops/status/example5',
    tweetDate: '2024-12-10',
    confidence: 9,
    novelty: 4,
  },
  {
    title: 'Academic literature synthesis',
    category: 'Research',
    subcategory: 'Literature review',
    description: 'PhD student uses Claude to synthesize literature across 200+ papers for thesis intro.',
    notes: 'Took 3 iterations with increasing context. Final output required ~30 min of editing.',
    sourceUrl: 'https://twitter.com/phd_student/status/example6',
    tweetDate: '2024-12-12',
    confidence: 7,
    novelty: 6,
  },
  {
    title: 'Real estate listing copywriting',
    category: 'Marketing',
    subcategory: 'Copywriting',
    description: 'Real estate agency generates listing descriptions from agent-provided bullet points.',
    notes: 'Saves ~45 min per listing. 200+ listings/month. Agents do light editing.',
    sourceUrl: 'https://twitter.com/proptech_news/status/example7',
    tweetDate: '2024-12-15',
    confidence: 8,
    novelty: 3,
  },
  {
    title: 'Competitive intelligence briefing',
    category: 'Strategy',
    subcategory: 'Competitive analysis',
    description: 'Startup uses Claude to compile weekly competitive intelligence reports from RSS and news.',
    notes: 'Aggregates 15 sources. Output is a Slack post with key moves and recommended responses.',
    sourceUrl: 'https://twitter.com/startup_ops/status/example8',
    tweetDate: '2025-01-03',
    confidence: 8,
    novelty: 7,
  },
  {
    title: 'Accessibility audit of UI components',
    category: 'Design',
    subcategory: 'Accessibility',
    description: 'Design team feeds component HTML to Claude to check WCAG compliance.',
    notes: 'Catches ~60% of issues flagged by axe-core. Useful for quick pre-commit checks.',
    sourceUrl: 'https://twitter.com/ux_eng/status/example9',
    tweetDate: '2025-01-10',
    confidence: 7,
    novelty: 7,
  },
]

// ---------------------------------------------------------------------------

async function seed() {
  const col = db.collection('use_cases')
  let inserted = 0
  let skipped = 0

  for (const record of SEED) {
    // Idempotency: skip if sourceUrl already exists
    const existing = await col.where('sourceUrl', '==', record.sourceUrl).limit(1).get()
    if (!existing.empty) {
      console.log(`  SKIP  ${record.title}`)
      skipped++
      continue
    }

    await col.add({
      ...record,
      tags: [],
      createdAt: admin.firestore.FieldValue.serverTimestamp(),
      updatedAt: admin.firestore.FieldValue.serverTimestamp(),
    })
    console.log(`  INSERT ${record.title}`)
    inserted++
  }

  console.log(`\nDone. ${inserted} inserted, ${skipped} skipped.`)
  process.exit(0)
}

seed().catch(err => {
  console.error(err)
  process.exit(1)
})
