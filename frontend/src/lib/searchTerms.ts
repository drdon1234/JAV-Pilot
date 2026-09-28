export function normalizeSearchQuery(value: string): string {
  const normalized = value.normalize('NFKC').trim().replace(/\s+/gu, ' ')
  if (!normalized) return ''
  const seen = new Set<string>()
  return normalized.split(' ').filter((term) => {
    const key = term.toLocaleLowerCase()
    if (seen.has(key)) return false
    seen.add(key)
    return true
  }).join(' ')
}

export function searchTerms(value: string): string[] {
  const normalized = normalizeSearchQuery(value).toLocaleLowerCase()
  return normalized ? normalized.split(' ') : []
}

export function matchesAllSearchTerms(value: string, query: string): boolean {
  const terms = searchTerms(query)
  if (!terms.length) return true
  const candidate = value.normalize('NFKC').toLocaleLowerCase()
  return terms.every((term) => candidate.includes(term))
}
