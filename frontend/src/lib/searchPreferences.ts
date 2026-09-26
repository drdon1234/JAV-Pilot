import type { SearchKind, WorkflowSearchDefaults } from '../types'

export type SiteSelectionMode = 'all' | 'custom'

export interface SearchFormPreferences {
  siteMode: SiteSelectionMode
  sources: string[]
  resultLimit: number
  pageSize: number
  fetchMagnets: boolean
  exactMatch: boolean
  searchKind: SearchKind
}

const STORAGE_KEY = 'jav-pilot:search-form'
const LEGACY_FETCH_MAGNETS_KEY = 'jav-pilot-search-fetch-magnets'
const PAGE_SIZES = new Set([10, 20, 50, 100])
const SEARCH_KINDS = new Set<SearchKind>(['keyword', 'code', 'actor', 'tag', 'series', 'maker', 'publisher', 'director'])

export const BUILTIN_SEARCH_PREFERENCES: SearchFormPreferences = {
  siteMode: 'all',
  sources: [],
  resultLimit: 100,
  pageSize: 20,
  fetchMagnets: true,
  exactMatch: false,
  searchKind: 'keyword',
}

function cleanPreferences(value: unknown, fallback: SearchFormPreferences): SearchFormPreferences {
  const input = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const resultLimit = Number(input.resultLimit)
  const pageSize = Number(input.pageSize)
  const sources = Array.isArray(input.sources)
    ? Array.from(new Set(input.sources.filter((item): item is string => typeof item === 'string' && /^[a-z0-9_-]{1,64}$/i.test(item))))
    : fallback.sources
  return {
    siteMode: input.siteMode === 'custom' || input.siteMode === 'all' ? input.siteMode : fallback.siteMode,
    sources,
    resultLimit: Number.isInteger(resultLimit) && resultLimit >= 1 && resultLimit <= 999 ? resultLimit : fallback.resultLimit,
    pageSize: PAGE_SIZES.has(pageSize) ? pageSize : fallback.pageSize,
    fetchMagnets: typeof input.fetchMagnets === 'boolean' ? input.fetchMagnets : fallback.fetchMagnets,
    exactMatch: typeof input.exactMatch === 'boolean' ? input.exactMatch : fallback.exactMatch,
    searchKind: SEARCH_KINDS.has(input.searchKind as SearchKind) ? input.searchKind as SearchKind : fallback.searchKind,
  }
}

/** Defaults configured under 系统管理 → 工作流默认参数, falling back to built-ins. */
export function configuredSearchDefaults(defaults: WorkflowSearchDefaults | null | undefined): SearchFormPreferences {
  if (!defaults) return BUILTIN_SEARCH_PREFERENCES
  return cleanPreferences({
    siteMode: defaults.site_mode,
    sources: defaults.sources,
    resultLimit: defaults.result_limit,
    pageSize: defaults.page_size,
    fetchMagnets: defaults.fetch_magnets,
    exactMatch: defaults.exact_match,
    searchKind: defaults.search_kind,
  }, BUILTIN_SEARCH_PREFERENCES)
}

/** The last values this browser used, layered over the configured defaults. */
export function loadSearchPreferences(defaults: SearchFormPreferences = BUILTIN_SEARCH_PREFERENCES): SearchFormPreferences {
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY)
    if (raw) return cleanPreferences(JSON.parse(raw), defaults)
    const legacyMagnets = globalThis.localStorage?.getItem(LEGACY_FETCH_MAGNETS_KEY)
    if (legacyMagnets === '0' || legacyMagnets === '1') {
      return { ...defaults, fetchMagnets: legacyMagnets === '1' }
    }
  } catch {
    // Storage can be unavailable in privacy-restricted contexts; defaults still apply.
  }
  return defaults
}

export function hasStoredSearchPreferences(): boolean {
  try {
    return Boolean(globalThis.localStorage?.getItem(STORAGE_KEY))
  } catch {
    return false
  }
}

export function saveSearchPreferences(update: Partial<SearchFormPreferences>): void {
  try {
    const storage = globalThis.localStorage
    if (!storage) return
    const current = loadSearchPreferences()
    storage.setItem(STORAGE_KEY, JSON.stringify(cleanPreferences({ ...current, ...update }, current)))
  } catch {
    // The in-memory form keeps working; only cross-visit memory is lost.
  }
}

export function clearSearchPreferences(): void {
  try {
    globalThis.localStorage?.removeItem(STORAGE_KEY)
    globalThis.localStorage?.removeItem(LEGACY_FETCH_MAGNETS_KEY)
  } catch {
    // Nothing to clear when storage is unavailable.
  }
}
