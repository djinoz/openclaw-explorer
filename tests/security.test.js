import assert from 'node:assert/strict'
import test from 'node:test'
import { readFileSync } from 'node:fs'
import { csvEscape, safeUrl, normalizeSuggestionUrl } from '../src/security.js'

test('CSV export escapes formulas and quotes', () => {
  assert.equal(csvEscape('hello'), 'hello')
  assert.equal(csvEscape('a,b'), '"a,b"')
  assert.equal(csvEscape('"quoted"'), '"""quoted"""')
  assert.equal(csvEscape('=cmd|\' /C calc!A0'), "'=cmd|' /C calc!A0")
})

test('URL helper only allows http(s)', () => {
  assert.equal(safeUrl('https://example.com/path'), 'https://example.com/path')
  assert.equal(safeUrl('javascript:alert(1)'), null)
  assert.equal(safeUrl('ftp://example.com'), null)
})

test('Suggestion URL normalizer strips fragments and trailing slashes', () => {
  assert.equal(normalizeSuggestionUrl('https://example.com/path/#frag'), 'https://example.com/path')
  assert.equal(normalizeSuggestionUrl('https://example.com/'), 'https://example.com/')
  assert.equal(normalizeSuggestionUrl('mailto:test@example.com'), null)
})

test('Firestore rules require verified owner writes', () => {
  const rules = readFileSync('firestore.rules', 'utf8')
  assert.match(rules, /email_verified\s*==\s*true/)
  assert.match(rules, /allow create, update, delete:\s*if isOwner\(\);/)
  assert.match(rules, /match \/suggestion_queue\/\{docId\}/)
  assert.match(rules, /allow read: if true;/)
})
