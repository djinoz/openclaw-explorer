export function getDeepLinkDocId(urlLike) {
  if (!urlLike) return null
  try {
    const url = new URL(urlLike)
    const id = url.searchParams.get('id')?.trim()
    return id || null
  } catch {
    return null
  }
}

export function selectDeepLinkedRecord(records, urlLike) {
  const id = getDeepLinkDocId(urlLike)
  if (!id) return null
  return records.find(record => record.id === id) ?? null
}
