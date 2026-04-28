const IMPORT_FIELDS = new Set([
  'category',
  'sourceUser',
  'description',
  'refUrls',
  'tweetDate',
  'searchDate',
  'notes',
  'uncertainty',
  'novelty',
])

const CSV_COL_MAP = {
  category: 'category',
  'source user': 'sourceUser',
  sourceuser: 'sourceUser',
  description: 'description',
  'one-sentence description': 'description',
  'reference urls': 'refUrls',
  'reference urls / tweets': 'refUrls',
  refurls: 'refUrls',
  'tweet date': 'tweetDate',
  tweetdate: 'tweetDate',
  'search date': 'searchDate',
  searchdate: 'searchDate',
  notes: 'notes',
  uncertainty: 'uncertainty',
  'uncertainty / confidence': 'uncertainty',
  novelty: 'novelty',
  'novelty / already documented': 'novelty',
}

const KNOWN_SOURCE_LINK_HEADERS = new Set([
  'reference urls',
  'reference urls / tweets',
  'refurls',
])

function normalizeHeader(header) {
  return header.trim().toLowerCase().replace(/"/g, '')
}

function parseCsvLine(line) {
  const vals = []
  let cur = ''
  let inQ = false
  for (const ch of line) {
    if (ch === '"') inQ = !inQ
    else if (ch === ',' && !inQ) {
      vals.push(cur)
      cur = ''
    } else {
      cur += ch
    }
  }
  vals.push(cur)
  return vals
}

function isSourceLinkHeader(header) {
  return /(?:^|\s)(?:ref|reference)\s*(?:url|urls|link|links)\b/.test(header)
}

function parseRows(text) {
  const lines = text.split('\n').filter(Boolean)
  if (!lines.length) return { headers: [], rows: [] }

  const headers = parseCsvLine(lines[0]).map(normalizeHeader)
  const rows = lines.slice(1).map(line => {
    const vals = parseCsvLine(line)
    const row = {}
    headers.forEach((header, i) => {
      const canonicalKey = CSV_COL_MAP[header]
      if (!canonicalKey || !IMPORT_FIELDS.has(canonicalKey)) return
      row[canonicalKey] = (vals[i] ?? '').trim()
    })
    return row
  }).filter(row => row.category || row.description)

  return { headers, rows }
}

export function parseCSV(text) {
  return parseRows(text).rows
}

export function prepareCsvImport(text) {
  const { headers, rows } = parseRows(text)
  const unknownSourceLinkHeaders = headers.filter(
    header => isSourceLinkHeader(header) && !KNOWN_SOURCE_LINK_HEADERS.has(header)
  )

  if (unknownSourceLinkHeaders.length) {
    return {
      rows: null,
      error: `Unrecognized source-link column: ${unknownSourceLinkHeaders.join(', ')}`,
    }
  }

  const missingRefUrlRows = rows
    .map((row, index) => ({ rowNumber: index + 2, row }))
    .filter(({ row }) => !String(row.refUrls || '').trim())

  if (missingRefUrlRows.length) {
    const rowNumbers = missingRefUrlRows.map(({ rowNumber }) => rowNumber)
    return {
      rows: null,
      error: `Missing Reference URLs in row${rowNumbers.length === 1 ? '' : 's'} ${rowNumbers.join(', ')}`,
    }
  }

  return { rows, error: null }
}
