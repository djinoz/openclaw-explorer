import assert from 'node:assert/strict'
import test from 'node:test'
import { parseCSV, prepareCsvImport } from '../src/csvImport.js'

test('parseCSV maps "Reference URLs / Tweets" into canonical refUrls', () => {
  const rows = parseCSV([
    'Category,Description,Reference URLs / Tweets,Novelty',
    'AI agents,Example use case,https://example.com/thread,highly novel',
  ].join('\n'))

  assert.equal(rows.length, 1)
  assert.deepEqual(rows[0], {
    category: 'AI agents',
    description: 'Example use case',
    refUrls: 'https://example.com/thread',
    novelty: 'highly novel',
  })
})

test('prepareCsvImport rejects unrecognized source-link columns', () => {
  const result = prepareCsvImport([
    'Category,Description,Reference Link',
    'AI agents,Example use case,https://example.com/thread',
  ].join('\n'))

  assert.equal(result.rows, null)
  assert.match(result.error, /unrecognized source-link column/i)
})

test('prepareCsvImport rejects rows missing canonical refUrls', () => {
  const result = prepareCsvImport([
    'Category,Description,Reference URLs',
    'AI agents,Example use case,',
  ].join('\n'))

  assert.equal(result.rows, null)
  assert.match(result.error, /missing reference urls/i)
})
