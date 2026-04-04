export function csvEscape(value) {
  const stringValue = String(value ?? '')
  const escaped = stringValue.replace(/"/g, '""')
  const safe = /^[\t\r ]*[=+\-@]/.test(escaped) ? `'${escaped}` : escaped
  return safe.includes(',') || safe.includes('"') || safe.includes('\n')
    ? `"${safe}"` : safe
}

export function safeUrl(value) {
  if (!value) return null
  try {
    const url = new URL(value)
    return ['http:', 'https:'].includes(url.protocol) ? url.href : null
  } catch {
    return null
  }
}

export function normalizeSuggestionUrl(value) {
  const safe = safeUrl(value)
  if (!safe) return null
  try {
    const url = new URL(safe)
    url.hash = ''
    if (url.pathname !== '/') url.pathname = url.pathname.replace(/\/+$/, '')
    return url.href
  } catch {
    return safe
  }
}
