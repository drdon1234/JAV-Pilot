import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronDown, Clapperboard, Database, ExternalLink, History, Languages, Layers3, RefreshCw, RotateCcw, Search, SlidersHorizontal, Trash2, X } from 'lucide-react'
import { type FormEvent, lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'

import { useToast } from '../components/ToastProvider'
import { BackToTopButton } from '../components/BackToTopButton'
import { ResultPagination } from '../components/ResultPagination'
import { Button, EmptyState, Field, InlineNotice, PageHeader, ProgressBar, SkeletonRows, StatusBadge, Toggle } from '../components/ui'
import { api, type PersistedMetadataSearchSession } from '../lib/api'
import { recoverableImport } from '../lib/recoverableImport'
import { clearPersistedDetailPrefetchBatchId, persistDetailPrefetchBatchId, storedDetailPrefetchBatchId } from '../lib/detailPrefetchSession'
import { serviceErrorMessage } from '../lib/presentation'
import {
  clearSearchPreferences,
  configuredSearchDefaults,
  hasStoredSearchPreferences,
  loadSearchPreferences,
  saveSearchPreferences,
  type SearchFormPreferences,
  type SiteSelectionMode,
} from '../lib/searchPreferences'
import { matchesAllSearchTerms, normalizeSearchQuery, searchTerms } from '../lib/searchTerms'
import { useTranslationPreferences, useTranslations } from '../lib/translation'
import { useAiTranslations } from '../lib/aiTranslation'
import { AiTranslateButton, AiTranslationLine } from '../components/AiTranslation'
import {
  searchSessionKey,
  searchSessionParams,
  type SearchSessionRequest,
  type SearchSessionStatus,
  useSearchSessions,
} from '../lib/searchSessions'
import type { DetailPrefetchBatch, SearchKind, SearchMatch, SearchSort, SiteFilter, WorkMagnet, WorkResult } from '../types'
import { QuickWebDownload } from './QuickWebDownload'
import { SearchResultCard } from './SearchResultCard'
import { SearchSourceSummary, summarizeSources } from './SearchSourceSummary'
import '../styles/search.css'
import { canonicalMagnetAction, type DownloadState } from './WorkUi'

const ResourceSearchWorkspace = lazy(() => recoverableImport(
  'src/pages/ResourceSearchWorkspace.tsx',
  'ResourceSearchWorkspace',
  () => import('./ResourceSearchWorkspace'),
).then((module) => ({ default: module.ResourceSearchWorkspace })))

type SearchStatus = 'idle' | SearchSessionStatus
type ResultSort = SearchSort | 'rating_desc'

const SORT_OPTIONS: { value: SearchSort; label: string }[] = [
  { value: 'relevance', label: '相关度' },
  { value: 'release_date_desc', label: '发行日期（新到旧）' },
  { value: 'release_date_asc', label: '发行日期（旧到新）' },
  { value: 'code_asc', label: '番号（升序）' },
  { value: 'code_desc', label: '番号（降序）' },
]
const RESULT_SORT_OPTIONS: { value: ResultSort; label: string }[] = [
  ...SORT_OPTIONS,
  { value: 'rating_desc', label: '评分（高到低）' },
]
const SEARCH_REQUEST_SORT: SearchSort = 'relevance'
const CODE_COLLATOR = new Intl.Collator('en', { numeric: true, sensitivity: 'base' })
const SEARCH_KIND_OPTIONS: { value: SearchKind; label: string }[] = [
  { value: 'keyword', label: '全部内容' },
  { value: 'code', label: '番号' },
  { value: 'actor', label: '演员' },
  { value: 'tag', label: '标签' },
  { value: 'series', label: '系列' },
  { value: 'maker', label: '制作商' },
  { value: 'publisher', label: '发行商' },
  { value: 'director', label: '导演' },
]
const MATCH_VALUES = new Set<SearchMatch>(['auto', 'exact', 'fuzzy'])
const PAGE_SIZE_VALUES = new Set([10, 20, 50, 100])
import { SEARCH_CAPABILITIES, SEARCH_PARSER_PROFILES } from '../lib/sources'
const RESULT_LIMIT_ERROR = '请输入 1 到 999 之间的整数'
const DETAIL_TAB_POLL_INTERVAL_MS = 1_000
const DETAIL_TAB_MAX_POLL_ERRORS = 10

interface DetailTargetContext {
  locationSearch: string
  locationPathname: string
  activeResultLimit: number
  query: string
  selectedSources: string[]
  pageSize: number
  page: number
  fetchMagnets: boolean
  sort: SearchSort
  match: SearchMatch
  filters: Array<[string, string]>
}

export function buildDetailTarget(result: WorkResult, context: DetailTargetContext) {
  const lookupCode = result.code || result.canonical_code || ''
  const detailParams = new URLSearchParams(context.locationSearch)
  detailParams.set('result_limit', String(context.activeResultLimit))
  if (!detailParams.get('q')) {
    detailParams.set('q', context.query.trim())
    detailParams.set('source', context.selectedSources.join(','))
    detailParams.set('result_limit', String(context.activeResultLimit))
    detailParams.set('page_size', String(context.pageSize))
    detailParams.set('page', String(context.page))
    detailParams.set('magnets', context.fetchMagnets ? '1' : '0')
    detailParams.set('sort', context.sort)
    detailParams.set('match', context.match)
    context.filters.forEach(([id, value]) => detailParams.set(`filter.${id}`, value))
  }
  if (lookupCode) detailParams.set('code', lookupCode)
  detailParams.delete('replacement_id')
  const detailQuery = detailParams.size ? `?${detailParams.toString()}` : ''
  const returnParams = new URLSearchParams(context.locationSearch)
  returnParams.delete('replacement_id')
  returnParams.set('result_limit', String(context.activeResultLimit))
  return {
    href: `/works/${encodeURIComponent(result.work_id)}${detailQuery}`,
    returnTo: `${context.locationPathname}?${returnParams.toString()}`,
  }
}

function routeNumber(params: URLSearchParams, key: string, fallback: number): number {
  const value = Number(params.get(key))
  return Number.isInteger(value) && value > 0 ? value : fallback
}

function routeResultLimit(params: URLSearchParams): number {
  const value = routeNumber(params, 'result_limit', 100)
  return value <= 999 ? value : 100
}

function parseResultLimit(value: string): number | null {
  if (!/^\d+$/.test(value.trim())) return null
  const parsed = Number(value)
  return Number.isInteger(parsed) && parsed >= 1 && parsed <= 999 ? parsed : null
}

function detailPrefetchIsActive(batch: DetailPrefetchBatch | undefined): boolean {
  return batch?.status === 'queued' || batch?.status === 'running'
}

function detailPrefetchStatusLabel(batch: DetailPrefetchBatch, cancelling: boolean): string {
  if (cancelling) return '正在取消后台解析'
  if (detailPrefetchIsActive(batch)) return '后台解析中'
  if (batch.status === 'failed') return '后台解析失败'
  if (batch.status === 'partial') return '后台解析部分完成'
  return '后台解析完成'
}

interface OpenedDetailTab {
  tab: Window
  workId: string
  href: string
}

function waitForDetailTabs(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds))
}

function navigateDetailTab(item: OpenedDetailTab): boolean {
  if (item.tab.closed) return false
  try {
    item.tab.location.replace(item.href)
    return true
  } catch {
    item.tab.close()
    return false
  }
}

async function loadDetailTabsFromBatch(batchId: string, openedTabs: OpenedDetailTab[]) {
  const pending = new Map(openedTabs.map((item) => [item.workId, item]))
  let pollErrors = 0
  while (pending.size) {
    try {
      const batch = await api.detailPrefetchBatch(batchId)
      pollErrors = 0
      batch.items?.forEach((item) => {
        if (item.status !== 'completed' && item.status !== 'failed') return
        const opened = pending.get(item.work_id)
        if (!opened) return
        navigateDetailTab(opened)
        pending.delete(item.work_id)
      })
      if (!detailPrefetchIsActive(batch)) break
    } catch {
      pollErrors += 1
      if (pollErrors >= DETAIL_TAB_MAX_POLL_ERRORS) break
    }
    await waitForDetailTabs(DETAIL_TAB_POLL_INTERVAL_MS)
  }
  for (const item of pending.values()) {
    navigateDetailTab(item)
  }
}

function routePageSize(params: URLSearchParams): number {
  const value = routeNumber(params, 'page_size', 20)
  return PAGE_SIZE_VALUES.has(value) ? value : 20
}

function routeSort(params: URLSearchParams): SearchSort {
  const value = params.get('sort') as SearchSort | null
  return SORT_OPTIONS.some((option) => option.value === value) ? value as SearchSort : 'relevance'
}

function routeResultSort(params: URLSearchParams): ResultSort {
  const value = params.get('result_sort') as ResultSort | null
  return RESULT_SORT_OPTIONS.some((option) => option.value === value) ? value as ResultSort : routeSort(params)
}

function fivePointRating(result: WorkResult, javDbSourceIds: ReadonlySet<string>) {
  return result.sources
    .map((source) => ({
      sourceId: source.source_id,
      value: source.details?.rating?.value,
      votes: source.details?.rating?.votes ?? null,
    }))
    .filter((rating): rating is { sourceId: string; value: number; votes: number | null } => (
      javDbSourceIds.has(rating.sourceId)
      && typeof rating.value === 'number'
      && Number.isFinite(rating.value)
      && rating.value >= 0
      && rating.value <= 5
    ))
    .sort((left, right) => (
      (right.votes ?? -1) - (left.votes ?? -1)
      || left.sourceId.localeCompare(right.sourceId)
    ))[0] ?? null
}

function parseMinimumRating(value: string): number | null | undefined {
  const clean = value.trim()
  if (!clean) return null
  if (!/^(?:\d+(?:\.\d*)?|\.\d+)$/.test(clean)) return undefined
  const parsed = Number(clean)
  return Number.isFinite(parsed) && parsed >= 0 && parsed <= 5 ? parsed : undefined
}


function sortSearchResults(
  results: WorkResult[],
  sort: ResultSort,
  javDbSourceIds: ReadonlySet<string>,
): WorkResult[] {
  if (sort === 'relevance') return results
  if (sort === 'rating_desc') {
    return results
      .map((result, index) => ({ result, index, rating: fivePointRating(result, javDbSourceIds)?.value }))
      .sort((left, right) => {
        if (left.rating === undefined) return right.rating === undefined ? left.index - right.index : 1
        if (right.rating === undefined) return -1
        return right.rating - left.rating || left.index - right.index
      })
      .map(({ result }) => result)
  }
  const descending = sort.endsWith('_desc')
  return results
    .map((result, index) => ({ result, index }))
    .sort((left, right) => {
      const leftValue = sort.startsWith('release_date_')
        ? left.result.release_date?.trim()
        : (left.result.canonical_code || left.result.code)?.trim()
      const rightValue = sort.startsWith('release_date_')
        ? right.result.release_date?.trim()
        : (right.result.canonical_code || right.result.code)?.trim()
      if (!leftValue) return rightValue ? 1 : left.index - right.index
      if (!rightValue) return -1
      const comparison = sort.startsWith('release_date_')
        ? leftValue.localeCompare(rightValue)
        : CODE_COLLATOR.compare(leftValue, rightValue)
      return comparison ? (descending ? -comparison : comparison) : left.index - right.index
    })
    .map(({ result }) => result)
}

function routeMatch(params: URLSearchParams): SearchMatch {
  const value = params.get('match') as SearchMatch | null
  return value && MATCH_VALUES.has(value) ? value : 'auto'
}

function routeSearchKind(params: URLSearchParams): SearchKind {
  const value = params.get('kind') as SearchKind | null
  return SEARCH_KIND_OPTIONS.some((option) => option.value === value) ? value as SearchKind : 'keyword'
}

function routeSemanticRefs(params: URLSearchParams): Record<string, string> {
  const refs: Record<string, string> = {}
  params.forEach((value, key) => {
    if (key.startsWith('ref.') && value) refs[key.slice(4)] = value
  })
  return refs
}

function routeFilters(params: URLSearchParams): Record<string, string> {
  const filters: Record<string, string> = {}
  params.forEach((value, key) => {
    if (key.startsWith('filter.')) filters[key.slice(7)] = value
  })
  return filters
}

function routeSelectionMode(params: URLSearchParams): SiteSelectionMode {
  if (params.get('site_mode') === 'all') return 'all'
  if (params.get('site_mode') === 'custom' || params.has('source')) return 'custom'
  return 'all'
}

function persistedSearchRequest(session: PersistedMetadataSearchSession): SearchSessionRequest {
  return {
    query: session.request.query,
    sources: session.request.sources,
    resultLimit: session.request.result_limit,
    fetchMagnets: session.request.fetch_magnets,
    filters: session.request.filters,
    sort: session.request.sort,
    match: session.request.match,
    searchKind: session.request.search_kind,
    semanticRefs: session.request.semantic_refs,
    continuationToken: session.request.continuation_token ?? undefined,
  }
}

function intersectFilters(filters: SiteFilter[][]): SiteFilter[] {
  if (!filters.length) return []
  return filters[0].filter((candidate) => filters.slice(1).every((items) => items.some((item) => item.id === candidate.id && item.type === candidate.type)))
}

export function SearchPage() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const navigate = useNavigate()
  const location = useLocation()
  const sessions = useSearchSessions()
  const isResultsRoute = location.pathname === '/results'
  const initialParams = useMemo(() => new URLSearchParams(location.search), [])
  const resourceWorkspace = !isResultsRoute && new URLSearchParams(location.search).get('workspace') === 'resource'
  const currentSearchHref = `${location.pathname}${location.search}`
  const metadataWorkspaceHref = useRef(isResultsRoute || !resourceWorkspace ? currentSearchHref : '/search')
  const resourceWorkspaceHref = useRef(resourceWorkspace ? currentSearchHref : '/search?workspace=resource')
  const routeQuery = initialParams.get('q')?.trim() ?? ''
  // /results is driven by its URL; a fresh /search form starts from the last
  // values used in this browser, layered over the configured defaults.
  const storedPreferences = useMemo(() => loadSearchPreferences(), [])
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const webDownloadService = useQuery({
    queryKey: ['web-download-service'],
    queryFn: () => api.webDownloads({ limit: 1 }),
    enabled: isResultsRoute,
    retry: false,
    staleTime: 15_000,
  })
  const webDownloadConfigured = webDownloadService.data?.configured === true
    && webDownloadService.data.enabled !== false
  const webDownloadReady = webDownloadConfigured && webDownloadService.data?.available !== false
  const latestMetadataSearch = useQuery({
    queryKey: ['metadata-search-session', 'latest'],
    queryFn: api.latestMetadataSearchSession,
    enabled: !isResultsRoute && !resourceWorkspace,
    retry: false,
  })
  const [detailPrefetchBatchId, setDetailPrefetchBatchId] = useState(storedDetailPrefetchBatchId)
  const [detailPrefetchSeed, setDetailPrefetchSeed] = useState<DetailPrefetchBatch | undefined>()
  const detailPrefetch = useQuery({
    queryKey: ['detail-prefetch-batch', detailPrefetchBatchId],
    queryFn: () => api.detailPrefetchBatch(detailPrefetchBatchId),
    enabled: Boolean(detailPrefetchBatchId),
    placeholderData: detailPrefetchSeed?.batch_id === detailPrefetchBatchId ? detailPrefetchSeed : undefined,
    refetchInterval: (query) => detailPrefetchIsActive(query.state.data) ? 1_000 : false,
    refetchIntervalInBackground: false,
    retry: false,
  })
  const detailPrefetchCreateLock = useRef(false)
  useEffect(() => {
    if (detailPrefetch.data && !detailPrefetchIsActive(detailPrefetch.data)) {
      clearPersistedDetailPrefetchBatchId()
    }
  }, [detailPrefetch.data])
  const [openingDetailTabs, setOpeningDetailTabs] = useState(false)
  const createDetailPrefetch = useMutation({
    mutationFn: ({ requestId, workIds, source }: { requestId: string; workIds: string[]; source: string }) => (
      api.createDetailPrefetchBatch(requestId, workIds, source)
    ),
    onSuccess: (batch) => {
      setDetailPrefetchSeed(batch)
      setDetailPrefetchBatchId(batch.batch_id)
      persistDetailPrefetchBatchId(batch.batch_id)
      toast.push(`已将 ${batch.total} 部作品交给后台解析`, 'success')
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
    onSettled: () => {
      detailPrefetchCreateLock.current = false
    },
  })
  const cancelDetailPrefetch = useMutation({
    mutationFn: (batchId: string) => api.cancelDetailPrefetchBatch(batchId),
    onSuccess: (batch) => {
      queryClient.setQueryData(['detail-prefetch-batch', batch.batch_id], batch)
      setDetailPrefetchSeed(batch)
      toast.push('已停止后台详情解析', 'success')
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const [query, setQuery] = useState(routeQuery)
  const [selectedSources, setSelectedSources] = useState<string[]>(() => (
    isResultsRoute
      ? initialParams.get('source')?.split(',').filter(Boolean) ?? []
      : storedPreferences.sources
  ))
  const [selectionMode, setSelectionMode] = useState<SiteSelectionMode>(() => (
    isResultsRoute ? routeSelectionMode(initialParams) : storedPreferences.siteMode
  ))
  const [filterValues, setFilterValues] = useState<Record<string, string>>(() => routeFilters(initialParams))
  const [resultLimit, setResultLimit] = useState(() => String(
    isResultsRoute ? routeResultLimit(initialParams) : storedPreferences.resultLimit,
  ))
  const [resultLimitError, setResultLimitError] = useState('')
  const [pageSize, setPageSize] = useState(() => (
    isResultsRoute ? routePageSize(initialParams) : storedPreferences.pageSize
  ))
  const [resultKeyword, setResultKeyword] = useState(initialParams.get('result_q')?.trim() ?? '')
  const [minimumRating, setMinimumRating] = useState(initialParams.get('min_rating')?.trim() ?? '')
  const [continuationLimit, setContinuationLimit] = useState(200)
  const [resultSort, setResultSort] = useState<ResultSort>(() => routeResultSort(initialParams))
  const [match, setMatch] = useState<SearchMatch>(() => (
    isResultsRoute ? routeMatch(initialParams) : storedPreferences.exactMatch ? 'exact' : 'auto'
  ))
  const [searchKind, setSearchKind] = useState<SearchKind>(() => (
    isResultsRoute ? routeSearchKind(initialParams) : storedPreferences.searchKind
  ))
  const [semanticRefs, setSemanticRefs] = useState<Record<string, string>>(() => routeSemanticRefs(initialParams))
  const [fetchMagnets, setFetchMagnets] = useState(() => (
    isResultsRoute ? initialParams.get('magnets') !== '0' : storedPreferences.fetchMagnets
  ))
  const formTouchedRef = useRef(hasStoredSearchPreferences())
  const pendingSubmitRef = useRef(false)
  const [awaitingSites, setAwaitingSites] = useState(false)
  const queryInputRef = useRef<HTMLInputElement | null>(null)
  const selectAllRef = useRef<HTMLInputElement | null>(null)
  const [downloadStates, setDownloadStates] = useState<Record<string, DownloadState>>({})
  const [sourceSelectionError, setSourceSelectionError] = useState('')
  const [clearingSearch, setClearingSearch] = useState(false)
  const clearingSearchRef = useRef(false)
  const [filtersOpen, setFiltersOpen] = useState(
    () => !(isResultsRoute && (globalThis.matchMedia?.('(max-width: 720px)')?.matches ?? false)),
  )
  const [mobileFilters, setMobileFilters] = useState(
    () => globalThis.matchMedia?.('(max-width: 720px)')?.matches ?? false,
  )
  const sourceSelectorRef = useRef<HTMLFieldSetElement | null>(null)
  const resultStartRef = useRef<HTMLDivElement | null>(null)

  useEffect(() => {
    if (resourceWorkspace) resourceWorkspaceHref.current = currentSearchHref
    else metadataWorkspaceHref.current = currentSearchHref
  }, [currentSearchHref, resourceWorkspace])

  const sites = useMemo(
    () => settings.data?.settings.sites.filter(
      (site) => site.enabled
        && SEARCH_CAPABILITIES.some((capability) => site.capabilities.includes(capability))
        && SEARCH_PARSER_PROFILES.has(site.parser_profile),
    ) ?? [],
    [settings.data],
  )

  const routeRequest = useMemo<SearchSessionRequest | null>(() => {
    if (!isResultsRoute) return null
    const params = new URLSearchParams(location.search)
    const queryValue = params.get('q')?.trim() ?? ''
    const configuredSourceIds = new Set(sites.map((site) => site.id))
    const sources = params.get('source')?.split(',').filter((source) => configuredSourceIds.has(source)) ?? []
    if (!queryValue || !sources.length) return null
    return {
      query: queryValue,
      sources,
      filters: routeFilters(params),
      resultLimit: routeResultLimit(params),
      fetchMagnets: params.get('magnets') !== '0',
      sort: routeSort(params),
      match: routeMatch(params),
      searchKind: routeSearchKind(params),
      semanticRefs: Object.fromEntries(
        Object.entries(routeSemanticRefs(params)).filter(([source]) => sources.includes(source)),
      ),
    }
  }, [isResultsRoute, location.search, sites])
  const sessionKey = routeRequest ? searchSessionKey(routeRequest) : ''
  const session = sessionKey ? sessions.getSession(sessionKey) : undefined
  const hasSession = Boolean(session)
  const sessionSourceIds = session?.request.sources ?? routeRequest?.sources ?? []
  const javDbSourceIds = useMemo(() => new Set(
    (settings.data?.settings.sites ?? [])
      .filter((site) => site.parser_profile === 'javdb' && sessionSourceIds.includes(site.id))
      .map((site) => site.id),
  ), [sessionSourceIds, settings.data?.settings.sites])
  const hasJavDbSource = javDbSourceIds.size > 0

  useEffect(() => {
    if (!sites.length) return
    setSelectedSources((current) => {
      const valid = current.filter((id) => sites.some((site) => site.id === id))
      return selectionMode === 'all' ? sites.map((site) => site.id) : valid
    })
  }, [selectionMode, sites])

  useEffect(() => {
    if (!isResultsRoute) return
    const params = new URLSearchParams(location.search)
    setQuery(params.get('q')?.trim() ?? '')
    setSelectionMode(routeSelectionMode(params))
    setSelectedSources(params.get('source')?.split(',').filter(Boolean) ?? [])
    setFilterValues(routeFilters(params))
    setResultLimit(String(routeResultLimit(params)))
    setResultLimitError('')
    setPageSize(routePageSize(params))
    setResultKeyword(params.get('result_q')?.trim() ?? '')
    setMinimumRating(params.get('min_rating')?.trim() ?? '')
    setResultSort(routeResultSort(params))
    setMatch(routeMatch(params))
    setSearchKind(routeSearchKind(params))
    setSemanticRefs(routeSemanticRefs(params))
    setFetchMagnets(params.get('magnets') !== '0')
    setSourceSelectionError('')
  }, [isResultsRoute, location.search])

  const configuredDefaults = settings.data?.settings.workflow_defaults?.search
  const translation = useTranslationPreferences(settings.data?.settings.workflow_defaults?.translation)
  useEffect(() => {
    // Configured defaults apply until this browser has its own remembered choice.
    if (isResultsRoute || formTouchedRef.current || !configuredDefaults) return
    applyPreferences(configuredSearchDefaults(configuredDefaults))
  }, [configuredDefaults, isResultsRoute])

  useEffect(() => {
    if (routeRequest && !clearingSearchRef.current) sessions.ensureSession(routeRequest)
  }, [clearingSearch, hasSession, routeRequest?.resultLimit, sessionKey, sessions.ensureSession])

  useEffect(() => {
    if (!isResultsRoute) clearingSearchRef.current = false
  }, [isResultsRoute])

  useEffect(() => {
    if (isResultsRoute && routeRequest && mobileFilters) setFiltersOpen(false)
  }, [isResultsRoute, mobileFilters, sessionKey])

  useEffect(() => setDownloadStates({}), [sessionKey])

  useEffect(() => {
    if (!session) return
    setContinuationLimit((current) => (
      current > session.progress.resultLimit
        ? current
        : Math.min(
          999,
          session.progress.resultLimit
            + Math.max(50, Math.min(200, session.progress.resultLimit)),
        )
    ))
  }, [session?.progress.resultLimit, session?.key])

  useEffect(() => {
    const media = globalThis.matchMedia?.('(max-width: 720px)')
    if (!media) return
    const update = (event: Pick<MediaQueryListEvent, 'matches'>) => {
      setMobileFilters(event.matches)
      if (!event.matches) setFiltersOpen(true)
    }
    update(media)
    media.addEventListener?.('change', update)
    return () => media.removeEventListener?.('change', update)
  }, [])

  const activeFilters = useMemo(() => {
    const definitions = sites.filter((site) => selectedSources.includes(site.id)).map((site) => site.filters ?? [])
    return intersectFilters(definitions)
  }, [selectedSources, sites])

  const status: SearchStatus = session?.status ?? (routeRequest ? 'searching' : 'idle')
  const results = session?.results ?? []
  const sourceErrors = session?.sourceErrors ?? {}
  const progress = session?.progress ?? {
    done: 0,
    total: 0,
    pagesScanned: 0,
    pagesTotal: null,
    found: 0,
    resultLimit: parseResultLimit(resultLimit) ?? 100,
  }
  const elapsed = session?.elapsed ?? 0
  const requestedPage = routeNumber(new URLSearchParams(location.search), 'page', 1)
  const cancellable = status === 'searching' || status === 'resolving'
  const busy = cancellable
  const resolvingProgress = status === 'resolving'
  const allSelected = sites.length > 0 && sites.every((site) => selectedSources.includes(site.id))
  const queryError = searchTerms(query).length > 16 ? '搜索最多支持 16 个关键词' : ''
  const parsedMinimumRating = parseMinimumRating(minimumRating)
  const minimumRatingError = parsedMinimumRating === undefined ? '请输入 0 到 5 之间的评分，留空表示不限' : ''
  const filteredResults = useMemo(() => {
    const keyword = normalizeSearchQuery(resultKeyword)
    const ratingThreshold = hasJavDbSource
      && parsedMinimumRating !== undefined
      && parsedMinimumRating !== null
      && parsedMinimumRating > 0
      ? parsedMinimumRating
      : null
    return results.filter((result) => {
      if (keyword && !matchesAllSearchTerms([
        result.code,
        result.canonical_code,
        result.title,
        ...result.actors,
        ...result.tags,
      ].map((value) => String(value ?? '')).join(' '), keyword)) return false
      if (ratingThreshold === null) return true
      const rating = fivePointRating(result, javDbSourceIds)
      return rating !== null && rating.value >= ratingThreshold
    })
  }, [hasJavDbSource, javDbSourceIds, parsedMinimumRating, resultKeyword, results])
  const sortedResults = useMemo(
    () => sortSearchResults(filteredResults, resultSort, javDbSourceIds),
    [filteredResults, javDbSourceIds, resultSort],
  )
  const pageCount = Math.max(1, Math.ceil(sortedResults.length / pageSize))
  const page = Math.min(requestedPage, pageCount)
  const visibleResults = sortedResults.slice((page - 1) * pageSize, page * pageSize)
  const titleTranslations = useTranslations(
    visibleResults.map((result) => result.title),
    translation.preferences.enabled,
  )
  const aiTitles = useAiTranslations(visibleResults.map((result) => result.title))

  function remember(update: Partial<SearchFormPreferences>) {
    formTouchedRef.current = true
    saveSearchPreferences(update)
  }

  function applyPreferences(preferences: SearchFormPreferences) {
    setSelectionMode(preferences.siteMode)
    setSelectedSources(preferences.siteMode === 'all' ? sites.map((site) => site.id) : preferences.sources)
    setResultLimit(String(preferences.resultLimit))
    setResultLimitError('')
    setPageSize(preferences.pageSize)
    setFetchMagnets(preferences.fetchMagnets)
    setMatch(preferences.exactMatch ? 'exact' : 'auto')
    setSearchKind(preferences.searchKind)
    setSourceSelectionError('')
  }

  function restoreDefaultPreferences() {
    clearSearchPreferences()
    formTouchedRef.current = false
    applyPreferences(configuredSearchDefaults(configuredDefaults))
    toast.push('已恢复默认搜索条件', 'success')
  }

  function updateSelection(next: string[]) {
    // Selecting every site is the same as 全选; any gap is a custom selection.
    const all = sites.length > 0 && sites.every((site) => next.includes(site.id))
    const mode: SiteSelectionMode = all ? 'all' : 'custom'
    const sources = all ? sites.map((site) => site.id) : next
    setSourceSelectionError('')
    setSelectionMode(mode)
    setSelectedSources(sources)
    remember({ siteMode: mode, sources: all ? [] : sources })
  }

  function toggleSource(id: string) {
    updateSelection(selectedSources.includes(id)
      ? selectedSources.filter((item) => item !== id)
      : [...selectedSources, id])
  }

  function toggleAllSources() {
    updateSelection(allSelected ? [] : sites.map((site) => site.id))
  }

  function changeFetchMagnets(value: boolean) {
    setFetchMagnets(value)
    remember({ fetchMagnets: value })
  }

  function changeExactMatch(value: boolean) {
    setMatch(value ? 'exact' : 'auto')
    remember({ exactMatch: value })
  }

  function draftRequest(validatedResultLimit: number): SearchSessionRequest {
    const filters = Object.fromEntries(
      activeFilters.map((filter) => [filter.id, filterValues[filter.id] ?? filter.default ?? '']),
    )
    return {
      query: normalizeSearchQuery(query),
      sources: [...selectedSources],
      filters,
      resultLimit: validatedResultLimit,
      sort: SEARCH_REQUEST_SORT,
      match,
      fetchMagnets,
      searchKind,
      semanticRefs,
    }
  }

  function navigateToResults(request: SearchSessionRequest, mode: SiteSelectionMode) {
    const routeParams = searchSessionParams(request)
    routeParams.set('site_mode', mode)
    routeParams.set('page', '1')
    routeParams.set('page_size', String(pageSize))
    if (resultSort !== request.sort) routeParams.set('result_sort', resultSort)
    if (parsedMinimumRating !== undefined && parsedMinimumRating !== null && parsedMinimumRating > 0) {
      routeParams.set('min_rating', String(parsedMinimumRating))
    }
    navigate(`/results?${routeParams.toString()}`)
  }

  function cancelSearch() {
    if (sessionKey) sessions.cancelSession(sessionKey)
  }

  function restoreLatestSearch() {
    const latest = latestMetadataSearch.data
    if (!latest) return
    const request = persistedSearchRequest(latest)
    if (!sessions.restoreSession(request, latest.request_id)) return
    const params = searchSessionParams(request)
    params.set('site_mode', 'custom')
    params.set('page', '1')
    params.set('page_size', String(pageSize))
    navigate(`/results?${params.toString()}`)
  }

  async function clearSearchResults() {
    if (clearingSearch) return
    clearingSearchRef.current = true
    setClearingSearch(true)
    try {
      const cleared = await sessions.clearSessions()
      toast.push(`已清空 ${cleared.cleared} 条搜索记录`, 'success')
      navigate('/search', { replace: true })
      void latestMetadataSearch.refetch()
    } catch (error) {
      clearingSearchRef.current = false
      toast.push((error as Error).message, 'error')
    } finally {
      setClearingSearch(false)
    }
  }

  function continueSearch() {
    if (!sessionKey || !session || session.continuationMode !== 'extend') return
    if (!sessions.continueSession(sessionKey, continuationLimit)) return
    const params = new URLSearchParams(location.search)
    params.set('result_limit', String(continuationLimit))
    navigate(`/results?${params.toString()}`, { replace: true })
  }

  function retryFailedSearch() {
    if (!sessionKey || !session || session.continuationMode !== 'retry') return
    sessions.continueSession(sessionKey, session.progress.resultLimit)
  }

  async function addDownload(result: WorkResult, magnet: WorkMagnet) {
    const action = canonicalMagnetAction(magnet)
    const source = action.primary
    if (!source || !action.uri) return
    setDownloadStates((current) => ({ ...current, [magnet.info_hash]: 'adding' }))
    try {
      const added = await api.addDownload({
        magnet: action.uri,
        name: magnet.display_name || source.display_name || result.code || result.title,
        category: '',
        save_path: '',
        tags: '',
        auto_organize: true,
        result,
        magnet_info: {
          uri: action.uri,
          info_hash: magnet.info_hash,
          display_name: magnet.display_name || source.display_name,
          trackers: action.trackers,
          exact_length: magnet.size_is_exact === true ? magnet.size_bytes : null,
          params: {},
          source_id: source.source_id,
        },
      })
      setDownloadStates((current) => ({ ...current, [magnet.info_hash]: 'added' }))
      const rule = added.organize?.name ? `，规则：${added.organize.name}` : ''
      const metadataNotice = added.metadata_warning ? '；自动元数据登记失败，完成后请手动补全' : ''
      toast.push(
        `已添加 ${added.display_name || result.code}${rule}${metadataNotice}`,
        added.metadata_warning ? 'info' : 'success',
      )
    } catch (error) {
      setDownloadStates((current) => ({ ...current, [magnet.info_hash]: 'error' }))
      toast.push((error as Error).message, 'error')
    }
  }



  function submit(event?: FormEvent) {
    event?.preventDefault()
    if (!query.trim()) {
      queryInputRef.current?.focus()
      return
    }
    if (queryError) return
    if (!sites.length) {
      // Site settings are still loading (common on a phone's first visit).
      // Remember the request instead of silently ignoring Enter.
      if (settings.isError) return
      pendingSubmitRef.current = true
      setAwaitingSites(true)
      return
    }
    const validatedResultLimit = parseResultLimit(resultLimit)
    if (validatedResultLimit === null) {
      setResultLimitError(RESULT_LIMIT_ERROR)
      setFiltersOpen(true)
      return
    }
    if (!selectedSources.length) {
      setSourceSelectionError('请至少选择一个搜索站点')
      setFiltersOpen(true)
      sourceSelectorRef.current?.focus()
      return
    }
    setResultLimitError('')
    setSourceSelectionError('')
    if (mobileFilters) setFiltersOpen(false)
    const request = draftRequest(validatedResultLimit)
    remember({
      siteMode: selectionMode,
      sources: selectionMode === 'all' ? [] : [...selectedSources],
      resultLimit: validatedResultLimit,
      pageSize,
      fetchMagnets,
      exactMatch: match === 'exact',
      searchKind,
    })
    sessions.refreshSession(request)
    navigateToResults(request, selectionMode)
  }

  useEffect(() => {
    if (!pendingSubmitRef.current || !sites.length) return
    // The 全选 selection is filled in by an earlier effect; wait for it.
    if (selectionMode === 'all' && !selectedSources.length) return
    pendingSubmitRef.current = false
    setAwaitingSites(false)
    submit()
  }, [sites.length, selectedSources, selectionMode])

  useEffect(() => {
    if (selectAllRef.current) selectAllRef.current.indeterminate = !allSelected && selectedSources.length > 0
  }, [allSelected, selectedSources.length])

  function changePage(targetPage: number, placement: 'top' | 'bottom') {
    if (!routeRequest) return
    const params = new URLSearchParams(location.search)
    params.set('page', String(Math.max(1, targetPage)))
    params.set('page_size', String(pageSize))
    if (resultKeyword.trim()) params.set('result_q', resultKeyword.trim())
    else params.delete('result_q')
    navigate(`/results?${params.toString()}`)
    if (placement === 'bottom') resultStartRef.current?.scrollIntoView?.({ block: 'start' })
  }

  function changePageSize(value: number) {
    setPageSize(value)
    if (!isResultsRoute) return
    const params = new URLSearchParams(location.search)
    params.set('page', '1')
    params.set('page_size', String(value))
    navigate(`/results?${params.toString()}`, { replace: true })
  }

  function changeResultKeyword(value: string) {
    setResultKeyword(value)
    if (!isResultsRoute) return
    const params = new URLSearchParams(location.search)
    params.set('page', '1')
    if (value.trim()) params.set('result_q', value.trim())
    else params.delete('result_q')
    navigate(`/results?${params.toString()}`, { replace: true })
  }

  function changeMinimumRating(value: string) {
    setMinimumRating(value)
    if (!isResultsRoute) return
    const params = new URLSearchParams(location.search)
    params.set('page', '1')
    if (value.trim()) params.set('min_rating', value.trim())
    else params.delete('min_rating')
    navigate(`/results?${params.toString()}`, { replace: true })
  }

  function changeResultSort(value: ResultSort) {
    setResultSort(value)
    if (!isResultsRoute) return
    const params = new URLSearchParams(location.search)
    params.set('page', '1')
    if (value === routeSort(params)) params.delete('result_sort')
    else params.set('result_sort', value)
    navigate(`/results?${params.toString()}`, { replace: true })
  }

  const statusTone = status === 'complete' ? 'success' : status === 'cancelled' || status === 'error' ? 'neutral' : 'info'
  const statusLabel: Record<SearchStatus, string> = {
    idle: '等待搜索',
    searching: '查询站点',
    resolving: '聚合作品与磁链',
    complete: '已完成',
    cancelled: '已取消',
    error: '已结束',
  }
  // Whole-search failures are only surfaced when they need an action from the
  // user (an expired continuation); per-source problems live in the summary.
  const continuationHint = /续搜/.test(sourceErrors.search ?? '') ? sourceErrors.search : ''
  const sourceSummary = summarizeSources(
    session?.request.sources ?? routeRequest?.sources ?? [],
    settings.data?.settings.sites ?? [],
    results,
    session?.skippedSources ?? {},
    sourceErrors,
    status === 'searching' || status === 'resolving',
  )
  const emptyStateTitle = status === 'cancelled' ? '搜索已取消' : status === 'complete' || status === 'error' ? '未找到匹配作品' : '输入番号或关键词'
  const emptyStateDescription = status === 'idle'
    ? '结果将在这里显示'
    : status === 'error'
      ? '部分来源暂时无法访问，可稍后重新搜索'
      : status === 'complete'
        ? '请尝试更换番号、关键词或搜索站点'
        : undefined
  const activeResultLimit = session?.request.resultLimit
    ?? session?.progress.resultLimit
    ?? routeRequest?.resultLimit
    ?? parseResultLimit(resultLimit)
    ?? 100
  const canRetryFailedPage = Boolean(
    !busy
    && session?.canContinue
    && session.continuationMode === 'retry'
    && session.continuationToken
  )
  const canExtendSearch = Boolean(
    !busy
    && session?.canContinue
    && session.continuationMode === 'extend'
    && session.continuationToken
    && activeResultLimit < 999
  )
  const detailTargetContext: DetailTargetContext = {
    locationSearch: location.search,
    locationPathname: location.pathname,
    activeResultLimit,
    query,
    selectedSources,
    pageSize,
    page,
    fetchMagnets,
    sort: session?.request.sort ?? routeRequest?.sort ?? SEARCH_REQUEST_SORT,
    match,
    filters: activeFilters.map((filter) => [
      filter.id,
      filterValues[filter.id] ?? filter.default ?? '',
    ]),
  }
  const detailPrefetchBatch = detailPrefetch.data
    ?? (detailPrefetchSeed?.batch_id === detailPrefetchBatchId ? detailPrefetchSeed : undefined)
  const detailPrefetchActive = detailPrefetchIsActive(detailPrefetchBatch)
  const detailPrefetchStatusPending = Boolean(detailPrefetchBatchId)
    && !detailPrefetchBatch
    && detailPrefetch.isPending
  const detailPrefetchCreationDisabled = detailPrefetchActive
    || detailPrefetchStatusPending
    || createDetailPrefetch.isPending
    || openingDetailTabs
  const detailPrefetchDone = detailPrefetchBatch
    ? detailPrefetchBatch.completed + detailPrefetchBatch.failed
    : 0
  function startDetailPrefetch(works: WorkResult[]) {
    if (detailPrefetchActive || detailPrefetchCreateLock.current) {
      toast.push('已有后台详情解析正在进行', 'info')
      return
    }
    if (!session?.requestId) {
      toast.push('当前搜索会话不可用，请重新搜索后再试', 'error')
      return
    }
    const workIds = Array.from(new Set(works.map((result) => result.work_id)))
    if (!workIds.length) return
    if (workIds.length > 999) {
      toast.push('一次最多解析 999 部作品，请先缩小筛选范围', 'error')
      return
    }
    const source = selectedSources.length === 1 ? selectedSources[0] : 'all'
    detailPrefetchCreateLock.current = true
    createDetailPrefetch.mutate({ requestId: session.requestId, workIds, source })
  }

  async function openCurrentPageDetails() {
    if (detailPrefetchActive || detailPrefetchCreateLock.current) {
      toast.push('已有后台详情解析正在进行', 'info')
      return
    }
    if (!session?.requestId) {
      toast.push('当前搜索会话不可用，请重新搜索后再试', 'error')
      return
    }
    detailPrefetchCreateLock.current = true
    setOpeningDetailTabs(true)
    const openedTabs: OpenedDetailTab[] = []
    const openedWorkIds: string[] = []
    visibleResults.forEach((result) => {
      const tab = window.open('', '_blank')
      if (!tab) return
      try {
        tab.opener = null
        openedTabs.push({
          tab,
          workId: result.work_id,
          href: buildDetailTarget(result, detailTargetContext).href,
        })
        openedWorkIds.push(result.work_id)
      } catch {
        tab.close()
      }
    })
    toast.push(
      openedTabs.length === visibleResults.length
        ? `已在后台打开 ${openedTabs.length} 个详情页`
        : `浏览器仅允许打开 ${openedTabs.length} / ${visibleResults.length} 个详情页`,
      openedTabs.length === visibleResults.length ? 'success' : 'info',
    )
    if (!openedTabs.length) {
      detailPrefetchCreateLock.current = false
      setOpeningDetailTabs(false)
      return
    }
    try {
      const source = selectedSources.length === 1 ? selectedSources[0] : 'all'
      const batch = await api.createDetailPrefetchBatch(
        session.requestId,
        openedWorkIds,
        source,
      )
      setDetailPrefetchSeed(batch)
      setDetailPrefetchBatchId(batch.batch_id)
      persistDetailPrefetchBatchId(batch.batch_id)
      void loadDetailTabsFromBatch(batch.batch_id, openedTabs)
    } catch (error) {
      toast.push((error as Error).message, 'error')
      openedTabs.forEach(navigateDetailTab)
    } finally {
      detailPrefetchCreateLock.current = false
      setOpeningDetailTabs(false)
    }
  }

  return (
    <div className="page search-page">
      <PageHeader
        title="作品搜索"
        description={resourceWorkspace ? '发现可下载的 Web 视频资源' : '跨站聚合资料与磁链'}
        actions={!resourceWorkspace ? (
          <>
            <StatusBadge tone={statusTone}>{statusLabel[status]}</StatusBadge>
            {!isResultsRoute && latestMetadataSearch.data ? (
              <Button type="button" size="small" variant="secondary" onClick={restoreLatestSearch}>
                <History aria-hidden="true" />
                恢复上次搜索
              </Button>
            ) : null}
            {(isResultsRoute && session) || (!isResultsRoute && latestMetadataSearch.data) ? (
              <Button type="button" size="small" variant="ghost" disabled={clearingSearch} onClick={() => void clearSearchResults()}>
                <Trash2 aria-hidden="true" />
                {clearingSearch ? '正在清空' : '清空搜索结果'}
              </Button>
            ) : null}
          </>
        ) : undefined}
      />

      <nav className="search-mode-tabs" aria-label="搜索类型">
        <Link
          to={metadataWorkspaceHref.current}
          aria-current={!resourceWorkspace ? 'page' : undefined}
        >
          <Database aria-hidden="true" />
          资料与磁链
          <span>{sites.length}</span>
        </Link>
        <Link
          to={resourceWorkspaceHref.current}
          aria-current={resourceWorkspace ? 'page' : undefined}
        >
          <Clapperboard aria-hidden="true" />
          Web 视频资源
          <span>{settings.data?.settings.sites.filter((site) => site.enabled && site.capabilities.includes('resource_search')).length ?? 0}</span>
        </Link>
      </nav>

      {resourceWorkspace ? (
        <Suspense fallback={<SkeletonRows count={4} />}><ResourceSearchWorkspace /></Suspense>
      ) : <div className="search-workspace">
        <form className="search-controls" onSubmit={submit}>
          <header className="search-panel-title">
            <SlidersHorizontal aria-hidden="true" />
            <h2>检索条件</h2>
          </header>
          {settings.isError ? (
            <InlineNotice tone="danger" role="alert">
              <strong>无法加载搜索配置</strong>
              <span>读取站点配置时发生错误：{serviceErrorMessage(settings.error, '无法连接服务，请检查网络后重试')}</span>
              <Button type="button" size="small" onClick={() => void settings.refetch()} disabled={settings.isFetching}>
                <RefreshCw className={settings.isFetching ? 'spin' : ''} aria-hidden="true" />
                重新加载
              </Button>
            </InlineNotice>
          ) : null}
          <div className="search-primary-row">
            <Field label="番号或关键词" className="search-query-field" error={queryError} errorId="search-query-error">
              <div className="input-with-icon">
                <Search aria-hidden="true" />
                <input
                  ref={queryInputRef}
                  value={query}
                  onChange={(event) => {
                    setQuery(event.target.value)
                    setSemanticRefs({})
                  }}
                  placeholder="请输入番号或关键词"
                  autoComplete="off"
                  enterKeyHint="search"
                  maxLength={80}
                  aria-invalid={Boolean(queryError)}
                  aria-describedby={queryError ? 'search-query-error' : awaitingSites ? 'search-awaiting-sites' : undefined}
                />
              </div>
            </Field>
            {cancellable ? (
              <Button
                type="button"
                variant="danger"
                onClick={(event) => {
                  event.preventDefault()
                  cancelSearch()
                }}
              >
                <X aria-hidden="true" />
                取消
              </Button>
            ) : (
              <Button type="submit" variant="primary" disabled={Boolean(queryError)} aria-busy={awaitingSites}>
                <Search aria-hidden="true" />
                {awaitingSites ? '准备中' : '搜索'}
              </Button>
            )}
          </div>
          {awaitingSites ? (
            <span className="field-hint" id="search-awaiting-sites" role="status">正在加载站点配置，完成后自动开始搜索</span>
          ) : null}

          <details
            className="search-filter-disclosure"
            open={filtersOpen}
          >
            <summary onClick={(event) => {
              event.preventDefault()
              setFiltersOpen((current) => !current)
            }}>
              <SlidersHorizontal aria-hidden="true" />
              <span>筛选条件</span>
              <small>{selectedSources.length || 0} 个站点</small>
              <ChevronDown aria-hidden="true" />
            </summary>
            <div className="search-filter-content">
              <div className="search-option-grid">
                <Field label="搜索类别">
                  <select
                    value={searchKind}
                    onChange={(event) => {
                      setSearchKind(event.target.value as SearchKind)
                      setSemanticRefs({})
                      remember({ searchKind: event.target.value as SearchKind })
                    }}
                  >
                    {SEARCH_KIND_OPTIONS.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
                  </select>
                </Field>
                <Field
                  label="结果上限"
                  error={resultLimitError}
                  errorId="search-result-limit-error"
                  hint={match === 'exact' ? '按精确匹配后的作品数计算' : undefined}
                >
                  <input
                    type="number"
                    min={1}
                    max={999}
                    value={resultLimit}
                    onChange={(event) => {
                      setResultLimit(event.target.value)
                      setResultLimitError('')
                      const parsed = parseResultLimit(event.target.value)
                      if (parsed !== null) remember({ resultLimit: parsed })
                    }}
                    aria-invalid={Boolean(resultLimitError)}
                    aria-describedby={resultLimitError ? 'search-result-limit-error' : undefined}
                  />
                </Field>
                <Field label="每页显示">
                  <select value={pageSize} onChange={(event) => { changePageSize(Number(event.target.value)); remember({ pageSize: Number(event.target.value) }) }}>
                    {[10, 20, 50, 100].map((value) => <option value={value} key={value}>{value}</option>)}
                  </select>
                </Field>
              </div>

              <fieldset
                className="source-selector"
                ref={sourceSelectorRef}
                tabIndex={-1}
                aria-invalid={sourceSelectionError ? 'true' : undefined}
                aria-describedby={sourceSelectionError ? 'source-selection-error' : undefined}
              >
                <legend>搜索站点</legend>
                <label className={`source-option source-option-all ${allSelected ? 'selected' : ''}`}>
                  <input ref={selectAllRef} type="checkbox" checked={allSelected} onChange={toggleAllSources} disabled={!sites.length} />
                  <span>全选</span>
                </label>
                {sites.map((site) => (
                  <label className={`source-option ${selectedSources.includes(site.id) ? 'selected' : ''}`} key={site.id}>
                    <input type="checkbox" checked={selectedSources.includes(site.id)} onChange={() => toggleSource(site.id)} />
                    <span>{site.name}</span>
                  </label>
                ))}
                {sourceSelectionError ? <span className="field-error" id="source-selection-error" role="alert">{sourceSelectionError}</span> : null}
              </fieldset>

              <div className="search-switches">
                <Toggle label="精确匹配" checked={match === 'exact'} onChange={(event) => changeExactMatch(event.target.checked)} aria-describedby="search-exact-hint" />
                <Toggle label="解析磁链" checked={fetchMagnets} onChange={(event) => changeFetchMagnets(event.target.checked)} />
                <Toggle
                  label="翻译标题和简介"
                  checked={translation.preferences.enabled}
                  onChange={(event) => translation.update({ enabled: event.target.checked })}
                />
                {translation.preferences.enabled ? (
                  <Toggle
                    label="显示原文"
                    checked={translation.preferences.showOriginal}
                    onChange={(event) => translation.update({ showOriginal: event.target.checked })}
                  />
                ) : null}
                <span className="field-hint" id="search-exact-hint">精确匹配只保留与输入番号前缀完全一致的作品，例如输入前缀时不会混入前缀相近的其他系列。</span>
                {!isResultsRoute ? (
                  <Button type="button" size="small" variant="ghost" className="search-restore-defaults" onClick={restoreDefaultPreferences}>
                    <RotateCcw aria-hidden="true" />
                    恢复默认条件
                  </Button>
                ) : null}
              </div>

              {activeFilters.length ? (
                <div className="filter-row">
                  {activeFilters.map((filter) => (
                    <Field label={filter.label || filter.id} key={filter.id}>
                      {filter.type === 'text' ? (
                        <input value={filterValues[filter.id] ?? filter.default ?? ''} onChange={(event) => setFilterValues((current) => ({ ...current, [filter.id]: event.target.value }))} maxLength={256} />
                      ) : (
                        <select value={filterValues[filter.id] ?? filter.default ?? ''} onChange={(event) => setFilterValues((current) => ({ ...current, [filter.id]: event.target.value }))}>
                          {filter.options.map((option) => <option value={option.value} key={`${filter.id}-${option.value}`}>{option.label}</option>)}
                        </select>
                      )}
                    </Field>
                  ))}
                </div>
              ) : null}
            </div>
          </details>
        </form>

        <div className="search-results-workspace">
          {busy ? (
            <section className="search-progress" aria-live="polite">
              <div>
                <strong>{statusLabel[status]}</strong>
                <span>
                  {resolvingProgress
                    ? `${progress.done} / ${progress.total} 部磁链`
                    : progress.pagesScanned
                      ? `${progress.pagesScanned}${progress.pagesTotal ? ` / ${progress.pagesTotal}` : ''} 页，${progress.found} / ${activeResultLimit} 部`
                      : '连接中'}
                </span>
              </div>
              <ProgressBar
                value={resolvingProgress
                  ? (progress.total ? progress.done / progress.total : 0)
                  : progress.pagesTotal
                    ? progress.pagesScanned / progress.pagesTotal
                    : Math.min(0.96, progress.found / Math.max(1, activeResultLimit))}
                label="搜索进度"
              />
            </section>
          ) : null}

          {continuationHint ? <InlineNotice tone="info" role="status">{continuationHint}</InlineNotice> : null}

          {canRetryFailedPage ? (
            <div className="search-continuation">
              <Button type="button" variant="primary" onClick={retryFailedSearch}>
                <RefreshCw aria-hidden="true" />
                从失败页重试
              </Button>
              <span>使用已保存的失败页游标按当前上限重试，现有结果、筛选和分页保持不变。</span>
            </div>
          ) : null}

          {canExtendSearch ? (
            <div className="search-continuation">
              <Field label="续搜后的总上限">
                <input
                  type="number"
                  min={activeResultLimit + 1}
                  max={999}
                  value={continuationLimit}
                  onChange={(event) => setContinuationLimit(Math.min(
                    999,
                    Math.max(activeResultLimit + 1, Number(event.target.value) || activeResultLimit + 1),
                  ))}
                />
              </Field>
              <Button
                type="button"
                variant="primary"
                onClick={continueSearch}
                disabled={continuationLimit <= activeResultLimit || continuationLimit > 999}
              >
                <Search aria-hidden="true" />
                继续搜索
              </Button>
              <span>从各站保存的下一页继续，现有结果、筛选和分页保持不变。</span>
            </div>
          ) : null}

          <section className="results-section">
            <div className="section-toolbar" ref={resultStartRef}>
              <div>
                <h2>聚合作品</h2>
                <span aria-live="polite">
                  {results.length
                    ? `${filteredResults.length} / ${results.length} 部，第 ${page} / ${pageCount} 页${elapsed ? `，${elapsed} ms` : ''}`
                    : '暂无结果'}
                </span>
                <SearchSourceSummary items={sourceSummary} />
              </div>
              {filteredResults.length || page > 1 ? (
                <ResultPagination
                  ariaLabel="聚合作品顶部分页"
                  page={page}
                  pageCount={pageCount}
                  onPageChange={(nextPage) => changePage(nextPage, 'top')}
                />
              ) : null}
            </div>

            {results.length ? (
              <div className="result-filter-bar">
                <Field label="筛选当前结果" className="result-keyword-filter">
                  <div className="input-with-icon">
                    <Search aria-hidden="true" />
                    <input
                      value={resultKeyword}
                      onChange={(event) => changeResultKeyword(event.target.value)}
                      placeholder="番号、标题、演员或标签"
                      maxLength={80}
                    />
                  </div>
                </Field>
                {hasJavDbSource ? (
                  <Field
                    label="最低评分"
                    className="result-rating-filter"
                    error={minimumRatingError}
                    errorId={minimumRatingError ? 'search-minimum-rating-error' : undefined}
                  >
                    <input
                      type="text"
                      inputMode="decimal"
                      value={minimumRating}
                      placeholder="不限"
                      maxLength={8}
                      aria-invalid={Boolean(minimumRatingError)}
                      aria-describedby={minimumRatingError ? 'search-minimum-rating-error' : undefined}
                      onChange={(event) => changeMinimumRating(event.target.value)}
                    />
                  </Field>
                ) : null}
                <Field label="排序方式" className="result-sort-filter">
                  <select
                    value={hasJavDbSource || resultSort !== 'rating_desc' ? resultSort : 'relevance'}
                    onChange={(event) => changeResultSort(event.target.value as ResultSort)}
                  >
                    {RESULT_SORT_OPTIONS
                      .filter((option) => option.value !== 'rating_desc' || hasJavDbSource)
                      .map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
                  </select>
                </Field>
                <div className="result-translation-controls">
                  <Button
                    type="button"
                    size="small"
                    variant={translation.preferences.enabled ? 'secondary' : 'ghost'}
                    aria-pressed={translation.preferences.enabled}
                    onClick={() => translation.update({ enabled: !translation.preferences.enabled })}
                  >
                    <Languages className={titleTranslations.loading ? 'spin' : ''} aria-hidden="true" />
                    {translation.preferences.enabled ? '显示原标题' : '翻译标题和简介'}
                  </Button>
                  {translation.preferences.enabled ? (
                    <Toggle
                      label="同时显示原文"
                      checked={translation.preferences.showOriginal}
                      onChange={(event) => translation.update({ showOriginal: event.target.checked })}
                    />
                  ) : null}
                  <AiTranslateButton state={aiTitles} />
                </div>
                <span>关键词与评分筛选、排序和翻页不会重新访问站点</span>
              </div>
            ) : null}

            {filteredResults.length ? (
              <div className="detail-prefetch-toolbar" aria-label="批量详情操作">
                <div className="detail-prefetch-actions">
                  <Button
                    type="button"
                    size="small"
                    variant="secondary"
                    disabled={detailPrefetchCreationDisabled || !visibleResults.length}
                    onClick={() => startDetailPrefetch(visibleResults)}
                  >
                    <Layers3 aria-hidden="true" />
                    解析当前页（{visibleResults.length}）
                  </Button>
                  <Button
                    type="button"
                    size="small"
                    variant="secondary"
                    disabled={detailPrefetchCreationDisabled || !sortedResults.length}
                    onClick={() => startDetailPrefetch(sortedResults)}
                  >
                    <Layers3 aria-hidden="true" />
                    解析全部（{sortedResults.length}）
                  </Button>
                  <Button
                    type="button"
                    size="small"
                    variant="secondary"
                    className="desktop-detail-opener"
                    disabled={detailPrefetchCreationDisabled || !visibleResults.length}
                    onClick={() => void openCurrentPageDetails()}
                  >
                    <ExternalLink aria-hidden="true" />
                    后台打开当前页（{visibleResults.length}）
                  </Button>
                  {detailPrefetchActive && detailPrefetchBatch ? (
                    <Button
                      type="button"
                      size="small"
                      variant="secondary"
                      disabled={cancelDetailPrefetch.isPending}
                      onClick={() => cancelDetailPrefetch.mutate(detailPrefetchBatch.batch_id)}
                    >
                      <X aria-hidden="true" />
                      {cancelDetailPrefetch.isPending ? '正在取消' : '取消解析'}
                    </Button>
                  ) : null}
                </div>
                {detailPrefetchBatch ? (
                  <div className="detail-prefetch-progress" aria-live="polite">
                    <ProgressBar
                      value={detailPrefetchBatch.total ? detailPrefetchDone / detailPrefetchBatch.total : 0}
                      label="详情解析进度"
                    />
                    <span>
                      {detailPrefetchStatusLabel(
                        detailPrefetchBatch,
                        cancelDetailPrefetch.isPending,
                      )}：
                      {detailPrefetchBatch.completed} / {detailPrefetchBatch.total}
                      {detailPrefetchBatch.failed ? `，失败 ${detailPrefetchBatch.failed}` : ''}
                    </span>
                  </div>
                ) : null}
              </div>
            ) : null}

            {status === 'searching' && !results.length ? <SkeletonRows count={5} /> : null}
            {!busy && !results.length ? <EmptyState title={emptyStateTitle} description={emptyStateDescription} /> : null}
            {!busy && results.length && !filteredResults.length ? <EmptyState title="没有符合筛选条件的作品" description="调整关键词或最低评分即可恢复当前搜索结果" /> : null}
            {visibleResults.length ? (
              <div className="result-list">
                {visibleResults.map((result) => {
                  const detailTarget = buildDetailTarget(result, detailTargetContext)
                  const webCode = result.canonical_code || result.code || ''
                  const translatedTitle = translation.preferences.enabled ? titleTranslations.translate(result.title) : null
                  const aiTitle = aiTitles.translate(result.title)
                  return (
                    <SearchResultCard
                      key={result.work_id}
                      titleContent={translatedTitle || aiTitle ? (
                        <>
                          {translatedTitle || result.title}
                          {translatedTitle && translation.preferences.showOriginal ? <small className="result-original-title">{result.title}</small> : null}
                          <AiTranslationLine text={aiTitle} />
                        </>
                      ) : undefined}
                      actions={webCode && webDownloadConfigured ? <QuickWebDownload code={webCode} disabled={!webDownloadReady} /> : null}
                      result={result}
                      sites={sites}
                      detailHref={detailTarget.href}
                      detailState={{ work: result, returnTo: detailTarget.returnTo, fromSearch: true }}
                      rating={fivePointRating(result, javDbSourceIds)}
                      downloadStates={downloadStates}
                      onDownload={(magnet) => void addDownload(result, magnet)}
                      onOpenDownloads={() => navigate('/downloads')}
                    />
                  )
                })}
              </div>
            ) : null}
            {visibleResults.length ? (
              <ResultPagination
                ariaLabel="聚合作品底部分页"
                page={page}
                pageCount={pageCount}
                className="result-pager-bottom"
                onPageChange={(nextPage) => changePage(nextPage, 'bottom')}
              />
            ) : null}
          </section>
        </div>
      </div>}
      <BackToTopButton />
    </div>
  )
}
