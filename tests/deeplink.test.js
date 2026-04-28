import assert from 'node:assert/strict'
import test from 'node:test'
import { getDeepLinkDocId, selectDeepLinkedRecord } from '../src/deeplink.js'

test('getDeepLinkDocId reads ?id= Firestore doc ids from absolute URLs', () => {
  assert.equal(
    getDeepLinkDocId('https://openclaw-explorer.web.app/?id=d4optZK4qZ3SebvIoGex'),
    'd4optZK4qZ3SebvIoGex'
  )
})

test('getDeepLinkDocId ignores blank or missing ids', () => {
  assert.equal(getDeepLinkDocId('https://openclaw-explorer.web.app/'), null)
  assert.equal(getDeepLinkDocId('https://openclaw-explorer.web.app/?id='), null)
})

test('selectDeepLinkedRecord selects the matching record by Firestore doc id', () => {
  const records = [
    { id: 'alpha', description: 'first' },
    { id: 'd4optZK4qZ3SebvIoGex', description: 'target' },
    { id: 'omega', description: 'last' },
  ]

  assert.deepEqual(
    selectDeepLinkedRecord(records, 'https://openclaw-explorer.web.app/?id=d4optZK4qZ3SebvIoGex'),
    records[1]
  )
})

test('selectDeepLinkedRecord returns null when the deep-linked record is absent', () => {
  const records = [{ id: 'alpha' }]
  assert.equal(
    selectDeepLinkedRecord(records, 'https://openclaw-explorer.web.app/?id=missing'),
    null
  )
})
