#!/usr/bin/env node
// Generates firestore.rules from firestore.rules.template by substituting OWNER_EMAIL.
// Reads .env.local if present; falls back to process.env.

import { readFileSync, writeFileSync, existsSync } from 'node:fs'
import { resolve } from 'node:path'

const envPath = resolve('.env.local')
if (existsSync(envPath)) {
  for (const line of readFileSync(envPath, 'utf8').split('\n')) {
    const m = line.match(/^([A-Z_][A-Z0-9_]*)=(.*)$/)
    if (m && !process.env[m[1]]) process.env[m[1]] = m[2].trim()
  }
}

const ownerEmail = process.env.OWNER_EMAIL
if (!ownerEmail) {
  console.error('Error: OWNER_EMAIL is not set. Add it to .env.local or export it before running.')
  process.exit(1)
}

const template = readFileSync('firestore.rules.template', 'utf8')
const rules = template.replace('{{OWNER_EMAIL}}', ownerEmail)
writeFileSync('firestore.rules', rules)
console.log(`firestore.rules generated (owner: ${ownerEmail})`)
