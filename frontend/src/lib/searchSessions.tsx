import {
  createContext,
  type ReactNode,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react'

import type { SearchRequest, SearchStreamEvent, WorkResult } from '../types'
import { api, ApiError, createSearchRequestId, streamSearch } from './api'
import { normalizeSearchQuery } from './searchTerms'
import { createSearchSnapshotCache, type SearchSnapshotCache } from './searchSnapshot'

export type SearchSessionRequest = Omit<SearchRequest, 'requestId'>
export type SearchSessionStatus = 'searching' | 'resolving' | 'complete' | 'cancelled' | 'error'

export interface SearchSession {
  key: string
  requestId: string
  request: SearchSessionRequest
  status: SearchSessionStatus
  results: WorkResult[]
  sourceErrors: Record<string, string>
  skippedSources: Record<string, string>
  progress: {
    done: number
    total: number
    pagesScanned: number
    pagesTotal: number | null
    found: number
    resultLimit: number
  }
  elapsed: number
  canContinue: boolean
  continuationToken: string | null
  continuationMode: 'retry' | 'extend' | null
}

interface ActiveSearch {
  controller: AbortController
  requestId: string
  startedAt: number
  cancelRequested: boolean
  retryTimer: ReturnType<typeof globalThis.setTimeout> | null
  retryDelayMs: number
  lastEventId: number
  subscriptionActive: boolean
  resumeSubscription: (() => void) | null
  session: SearchSession | null
}

interface SearchSessionsValue {
  revision: number
  getSession: (key: string) => SearchSession | undefined
  ensureSession: (request: SearchSessionRequest) => string
  cancelSession: (key: string) => void
  refreshSession: (request: SearchSessionRequest) => string
  continueSession: (key: string, resultLimit: number) => boolean
  clearSessions: () => Promise<{ cleared: number; cancelled: number }>
  restoreSession: (request: SearchSessionRequest, requestId: string) => boolean
}

const SearchSessionsContext = createContext<SearchSessionsValue | null>(null)

function clockNow(): number {
  return typeof performance !== 'undefined' && typeof performance.now === 'function'
    ? performance.now()
    : Date.now()
}

function uniqueWorks(results: WorkResult[]): WorkResult[] {
  const works = new Map<string, WorkResult>()
  results.forEach((result) => works.set(result.work_id, result))
  return Array.from(works.values())
}

function upsertWork(results: WorkResult[], result: WorkResult): WorkResult[] {
  const index = results.findIndex((candidate) => candidate.work_id === result.work_id)
  if (index < 0) return [...results, result]
  const next = [...results]
  next[index] = result
  return next
}

export function normalizeSearchSessionRequest(request: SearchSessionRequest): SearchSessionRequest {
  const filters = Object.fromEntries(
    Object.entries(request.filters)
      .map(([key, value]) => [key.trim(), value] as const)
      .filter(([key]) => Boolean(key))
      .sort(([left], [right]) => left.localeCompare(right)),
  )
  const semanticRefs = Object.fromEntries(
    Object.entries(request.semanticRefs ?? {})
      .map(([key, value]) => [key.trim(), value.trim()] as const)
      .filter(([key, value]) => Boolean(key && value))
      .sort(([left], [right]) => left.localeCompare(right)),
  )
  return {
    ...request,
    query: normalizeSearchQuery(request.query),
    sources: Array.from(new Set(request.sources.map((source) => source.trim()).filter(Boolean))).sort(),
    filters,
    searchKind: request.searchKind ?? 'keyword',
    semanticRefs,
    continuationToken: /^[A-Za-z0-9_-]{24,64}$/.test(request.continuationToken ?? '')
      ? request.continuationToken
      : undefined,
  }
}

export function searchSessionParams(request: SearchSessionRequest): URLSearchParams {
  const normalized = normalizeSearchSessionRequest(request)
  const params = new URLSearchParams({
    q: normalized.query,
    source: normalized.sources.join(','),
    result_limit: String(normalized.resultLimit),
    magnets: normalized.fetchMagnets ? '1' : '0',
    sort: normalized.sort,
    match: normalized.match,
  })
  if (normalized.searchKind && normalized.searchKind !== 'keyword') params.set('kind', normalized.searchKind)
  Object.entries(normalized.semanticRefs ?? {}).forEach(([source, value]) => params.set(`ref.${source}`, value))
  Object.entries(normalized.filters).forEach(([key, value]) => params.set(`filter.${key}`, value))
  return params
}

export function searchSessionKey(request: SearchSessionRequest): string {
  const params = searchSessionParams(request)
  params.delete('result_limit')
  return params.toString()
}

function persistedSearchRequestKey(request: SearchSessionRequest): string {
  return searchSessionParams(request).toString()
}

function eventContinuation(
  session: SearchSession,
  payload: {
    can_continue?: boolean
    continuation_token?: string | null
    continuation_mode?: 'retry' | 'extend' | null
  },
): Pick<SearchSession, 'canContinue' | 'continuationToken' | 'continuationMode'> {
  if (
    payload.can_continue === undefined
    && payload.continuation_token === undefined
    && payload.continuation_mode === undefined
  ) {
    return {
      canContinue: session.canContinue,
      continuationToken: session.continuationToken,
      continuationMode: session.continuationMode,
    }
  }
  const token = typeof payload.continuation_token === 'string'
    && /^[A-Za-z0-9_-]{24,64}$/.test(payload.continuation_token)
    ? payload.continuation_token
    : null
  const mode = payload.continuation_mode === 'retry' || payload.continuation_mode === 'extend'
    ? payload.continuation_mode
    : null
  const canContinue = payload.can_continue === true && token !== null && mode !== null
  return {
    canContinue,
    continuationToken: canContinue ? token : null,
    continuationMode: canContinue ? mode : null,
  }
}

export function reduceSearchSession(session: SearchSession, event: SearchStreamEvent): SearchSession {
  if (event.event === 'source') {
    return {
      ...session,
      status: 'searching',
      results: uniqueWorks(event.payload.results),
      sourceErrors: event.payload.errors ?? session.sourceErrors,
      skippedSources: event.payload.skipped ?? session.skippedSources,
      progress: {
        ...session.progress,
        done: event.payload.completed_sources,
        total: event.payload.total_sources,
        found: event.payload.results.length,
      },
    }
  }
  if (event.event === 'delta') {
    const results = event.payload.delta.reduce(upsertWork, session.results)
    return {
      ...session,
      status: 'searching',
      results,
      progress: {
        done: event.payload.pages_scanned,
        total: event.payload.pages_total ?? 0,
        pagesScanned: event.payload.pages_scanned,
        pagesTotal: event.payload.pages_total,
        found: event.payload.found_count,
        resultLimit: event.payload.result_limit,
      },
    }
  }
  if (event.event === 'base') {
    const results = uniqueWorks(event.payload.results)
    const pagesScanned = event.payload.pages_scanned ?? session.progress.pagesScanned
    const pagesTotal = event.payload.pages_total === undefined
      ? session.progress.pagesTotal
      : event.payload.pages_total
    return {
      ...session,
      status: session.request.fetchMagnets && results.length ? 'resolving' : 'searching',
      results,
      sourceErrors: event.payload.errors ?? {},
      skippedSources: event.payload.skipped ?? session.skippedSources,
      progress: {
        done: 0,
        total: results.length,
        pagesScanned,
        pagesTotal,
        found: event.payload.found_count ?? results.length,
        resultLimit: event.payload.result_limit ?? session.request.resultLimit,
      },
      ...eventContinuation(session, event.payload),
    }
  }
  if (event.event === 'result') {
    return {
      ...session,
      results: upsertWork(session.results, event.payload.result),
      progress: {
        ...session.progress,
        done: event.payload.done,
        total: event.payload.total,
      },
    }
  }
  if (event.event === 'done') {
    return {
      ...session,
      status: 'complete',
      sourceErrors: event.payload.errors ?? session.sourceErrors,
      skippedSources: event.payload.skipped ?? session.skippedSources,
      progress: {
        ...session.progress,
        done: event.payload.done,
        total: event.payload.total,
        pagesScanned: event.payload.pages_scanned ?? session.progress.pagesScanned,
        pagesTotal: event.payload.pages_total === undefined
          ? session.progress.pagesTotal
          : event.payload.pages_total,
        found: event.payload.found_count ?? session.progress.found,
        resultLimit: event.payload.result_limit ?? session.progress.resultLimit,
      },
      ...eventContinuation(session, event.payload),
    }
  }
  if (event.event === 'cancelled') {
    return {
      ...session,
      status: 'cancelled',
      sourceErrors: event.payload.errors ?? session.sourceErrors,
      skippedSources: event.payload.skipped ?? session.skippedSources,
      progress: {
        ...session.progress,
        done: event.payload.done,
        total: event.payload.total,
        pagesScanned: event.payload.pages_scanned ?? session.progress.pagesScanned,
        pagesTotal: event.payload.pages_total === undefined
          ? session.progress.pagesTotal
          : event.payload.pages_total,
        found: event.payload.found_count ?? session.progress.found,
        resultLimit: event.payload.result_limit ?? session.progress.resultLimit,
      },
      ...eventContinuation(session, event.payload),
    }
  }
  if (event.event === 'error') {
    // Stored/background sessions surface continuation failures as SSE ``error``
    // events (the HTTP ``ApiError`` path in ``failSession`` is unreachable here),
    // so mirror that recovery off the backend-preserved ``code``.
    const continuationInvalid = event.payload.code === 'continuation_invalid'
    const continuationBusy = event.payload.code === 'continuation_in_progress'
    return {
      ...session,
      status: 'error',
      sourceErrors: {
        search: continuationInvalid
          ? `${event.payload.error}；续搜位置已失效，请重新搜索`
          : continuationBusy
            ? '续搜请求仍在进行，请稍后重试'
            : event.payload.error,
      },
      ...(continuationInvalid ? {
        canContinue: false,
        continuationToken: null,
        continuationMode: null,
      } : continuationBusy ? {
        // The requested target limit is already authoritative. A busy
        // continuation must retry that same target instead of extending it.
        continuationMode: 'retry' as const,
      } : {}),
    }
  }
  return session
}

function eventMatchesRequest(event: SearchStreamEvent, requestId: string): boolean {
  if (!('request_id' in event.payload)) return true
  return !event.payload.request_id || event.payload.request_id === requestId
}

function isTerminalSearchEvent(event: SearchStreamEvent): boolean {
  return event.event === 'done' || event.event === 'cancelled' || event.event === 'error'
}

function clearRetryTimer(active: ActiveSearch) {
  if (active.retryTimer === null) return
  globalThis.clearTimeout(active.retryTimer)
  active.retryTimer = null
}

const SEARCH_REQUEST_STORAGE_PREFIX = 'jav-pilot:metadata-search:'
const MAX_STORED_SEARCH_REQUESTS = 24

function storedSearchRequestId(key: string): string | null {
  try {
    const value = globalThis.localStorage?.getItem(`${SEARCH_REQUEST_STORAGE_PREFIX}${key}`) ?? ''
    if (/^[A-Za-z0-9_.-]{8,80}$/.test(value)) return value
    const parsed = JSON.parse(value) as { requestId?: unknown }
    return typeof parsed.requestId === 'string' && /^[A-Za-z0-9_.-]{8,80}$/.test(parsed.requestId)
      ? parsed.requestId
      : null
  } catch {
    return null
  }
}

function storeSearchRequestId(key: string, requestId: string): void {
  try {
    const storage = globalThis.localStorage
    if (!storage) return
    storage.setItem(
      `${SEARCH_REQUEST_STORAGE_PREFIX}${key}`,
      JSON.stringify({ requestId, savedAt: Date.now() }),
    )
    const stored: Array<{ key: string; savedAt: number }> = []
    for (let index = 0; index < storage.length; index += 1) {
      const storageKey = storage.key(index)
      if (!storageKey?.startsWith(SEARCH_REQUEST_STORAGE_PREFIX)) continue
      let savedAt = 0
      try {
        savedAt = Number((JSON.parse(storage.getItem(storageKey) ?? '') as { savedAt?: unknown }).savedAt) || 0
      } catch { /* legacy entries sort first */ }
      stored.push({ key: storageKey, savedAt })
    }
    stored.sort((left, right) => right.savedAt - left.savedAt)
    stored.slice(MAX_STORED_SEARCH_REQUESTS).forEach((item) => storage.removeItem(item.key))
  } catch {
    // Search still works when storage is unavailable; only cross-reload restoration is lost.
  }
}

function clearStoredSearchRequestIds(): void {
  try {
    const storage = globalThis.localStorage
    if (!storage) return
    const keys: string[] = []
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index)
      if (key?.startsWith(SEARCH_REQUEST_STORAGE_PREFIX)) keys.push(key)
    }
    keys.forEach((key) => storage.removeItem(key))
  } catch {
    // The server-side clear remains authoritative.
  }
}

export function SearchSessionProvider({
  children,
  maxEntries = 12,
  ttlMs = 30 * 60 * 1_000,
  maxActive = 2,
}: {
  children: ReactNode
  maxEntries?: number
  ttlMs?: number
  maxActive?: number
}) {
  const cacheRef = useRef<SearchSnapshotCache<SearchSession> | null>(null)
  if (!cacheRef.current) {
    cacheRef.current = createSearchSnapshotCache<SearchSession>({ maxEntries, ttlMs })
  }
  const activeRef = useRef(new Map<string, ActiveSearch>())
  const [revision, setRevision] = useState(0)

  const notify = useCallback(() => {
    setRevision((current) => current + 1)
  }, [])

  const getSession = useCallback((key: string) => (
    cacheRef.current?.read(key) ?? activeRef.current.get(key)?.session ?? undefined
  ), [])

  const rememberSession = useCallback((key: string, session: SearchSession) => {
    cacheRef.current?.remember(key, session)
    const active = activeRef.current.get(key)
    if (active?.requestId === session.requestId) active.session = session
    notify()
  }, [notify])

  const abortSession = useCallback((key: string, markCancelled: boolean) => {
    const active = activeRef.current.get(key)
    if (!active) return
    clearRetryTimer(active)
    activeRef.current.delete(key)
    active.controller.abort()
    void api.cancelSearch(active.requestId).catch(() => undefined)
    if (!markCancelled) return
    const current = cacheRef.current?.read(key)
    if (current?.requestId === active.requestId) {
      rememberSession(key, {
        ...current,
        status: 'cancelled',
        elapsed: Math.round(clockNow() - active.startedAt),
      })
    }
  }, [rememberSession])

  const cancelSession = useCallback((key: string) => {
    const active = activeRef.current.get(key)
    if (!active || active.cancelRequested) return
    const current = cacheRef.current?.read(key)
    if (!current || current.requestId !== active.requestId) return
    active.cancelRequested = true
    clearRetryTimer(active)
    activeRef.current.delete(key)
    active.controller.abort()
    rememberSession(key, {
      ...current,
      status: 'cancelled',
      elapsed: Math.round(clockNow() - active.startedAt),
    })
    void api.cancelSearch(active.requestId).then((payload) => {
      const latest = cacheRef.current?.read(key)
      if (payload.cancelled || latest?.requestId !== active.requestId) return
      rememberSession(key, {
        ...latest,
        status: 'cancelled',
        sourceErrors: {
          ...latest.sourceErrors,
          search: '后台取消未确认，任务可能已经结束',
        },
      })
    }).catch((error: Error) => {
      const latest = cacheRef.current?.read(key)
      if (latest?.requestId !== active.requestId) return
      rememberSession(key, {
        ...latest,
        status: 'cancelled',
        sourceErrors: {
          ...latest.sourceErrors,
          search: `后台取消未确认：${error.message}`,
        },
      })
    })
  }, [rememberSession])

  const startSession = useCallback((
    rawRequest: SearchSessionRequest,
    seed?: SearchSession,
    reusePersistedRequestId = true,
  ): string => {
    const request = normalizeSearchSessionRequest(rawRequest)
    const key = searchSessionKey(request)
    if (!request.query || !request.sources.length) return key
    if (activeRef.current.has(key)) return key

    while (activeRef.current.size >= Math.max(1, maxActive)) {
      const oldest = activeRef.current.keys().next().value
      if (typeof oldest !== 'string') break
      abortSession(oldest, true)
    }

    const persistenceKey = persistedSearchRequestKey(request)
    const requestId = reusePersistedRequestId
      ? storedSearchRequestId(persistenceKey) ?? createSearchRequestId()
      : createSearchRequestId()
    storeSearchRequestId(persistenceKey, requestId)
    const controller = new AbortController()
    const startedAt = clockNow()
    activeRef.current.set(key, {
      controller,
      requestId,
      startedAt,
      cancelRequested: false,
      retryTimer: null,
      retryDelayMs: 250,
      lastEventId: 0,
      subscriptionActive: false,
      resumeSubscription: null,
      session: null,
    })
    rememberSession(key, {
      key,
      requestId,
      request,
      status: 'searching',
      results: seed?.results ?? [],
      sourceErrors: seed?.sourceErrors ?? {},
      skippedSources: seed?.skippedSources ?? {},
      progress: {
        done: seed?.progress.done ?? 0,
        total: seed?.progress.total ?? 0,
        pagesScanned: seed?.progress.pagesScanned ?? 0,
        pagesTotal: seed?.progress.pagesTotal ?? null,
        found: seed?.progress.found ?? 0,
        // The request limit is authoritative as soon as a continuation starts.
        // Keeping the seed limit here creates a short 200 -> 300 race where
        // navigation can persist the stale limit before the first SSE event.
        resultLimit: request.resultLimit,
      },
      elapsed: 0,
      canContinue: seed?.canContinue ?? false,
      continuationToken: request.continuationToken ?? seed?.continuationToken ?? null,
      continuationMode: seed?.continuationMode ?? null,
    })

    const failSession = (error: Error) => {
      const active = activeRef.current.get(key)
      const current = cacheRef.current?.read(key) ?? active?.session ?? undefined
      if (!current || current.requestId !== requestId || active?.requestId !== requestId) return
      clearRetryTimer(active)
      activeRef.current.delete(key)
      const continuationInvalid = Boolean(
        request.continuationToken
        && error instanceof ApiError
        && error.status === 409
        && error.code === 'continuation_invalid',
      )
      const continuationBusy = Boolean(
        request.continuationToken
        && error instanceof ApiError
        && error.status === 409
        && error.code === 'continuation_in_progress',
      )
      rememberSession(key, {
        ...current,
        status: 'error',
        sourceErrors: {
          search: continuationInvalid
            ? `${error.message}；续搜位置已失效，请重新搜索`
            : continuationBusy
              ? '续搜请求仍在进行，请稍后重试'
              : error.message,
        },
        elapsed: Math.round(clockNow() - startedAt),
        ...(continuationInvalid ? {
          canContinue: false,
          continuationToken: null,
          continuationMode: null,
        } : continuationBusy ? {
          // The requested target limit is already authoritative. A busy
          // continuation must retry that same target instead of extending it.
          continuationMode: 'retry',
        } : {}),
      })
    }

    const scheduleReconnect = () => {
      const active = activeRef.current.get(key)
      if (
        active?.requestId !== requestId
        || active.controller.signal.aborted
        || active.cancelRequested
        || active.retryTimer !== null
      ) return
      const delay = active.retryDelayMs
      active.retryDelayMs = Math.min(5_000, Math.max(250, delay * 2))
      active.retryTimer = globalThis.setTimeout(() => {
        const latest = activeRef.current.get(key)
        if (latest?.requestId !== requestId || latest.controller.signal.aborted) return
        latest.retryTimer = null
        if (latest.cancelRequested) return
        connect()
      }, delay)
    }

    const connect = () => {
      const active = activeRef.current.get(key)
      if (
        active?.requestId !== requestId
        || active.controller.signal.aborted
        || active.cancelRequested
        || active.subscriptionActive
      ) return
      active.subscriptionActive = true
      void streamSearch(
        { ...request, requestId },
        controller.signal,
        (event) => {
          const latest = activeRef.current.get(key)
          if (
            latest?.requestId !== requestId
            || latest.cancelRequested
            || !eventMatchesRequest(event, requestId)
          ) return
          const current = cacheRef.current?.read(key) ?? latest.session ?? undefined
          if (!current || current.requestId !== requestId) return
          latest.retryDelayMs = 250
          const reduced = reduceSearchSession(current, event)
          rememberSession(key, isTerminalSearchEvent(event)
            ? { ...reduced, elapsed: Math.round(clockNow() - startedAt) }
            : reduced)
          if (isTerminalSearchEvent(event)) {
            clearRetryTimer(latest)
            activeRef.current.delete(key)
          }
        },
        {
          afterEventId: active.lastEventId,
          onCursor: (eventId) => {
            const latest = activeRef.current.get(key)
            if (latest?.requestId === requestId) {
              latest.lastEventId = Math.max(latest.lastEventId, eventId)
            }
          },
        },
      ).then(() => {
        const latest = activeRef.current.get(key)
        if (latest?.requestId !== requestId) return
        latest.subscriptionActive = false
        scheduleReconnect()
      }).catch((error: Error) => {
        const latest = activeRef.current.get(key)
        if (latest?.requestId === requestId) latest.subscriptionActive = false
        if (error.name === 'AbortError') return
        const fatalClientError = error instanceof ApiError && (
          (error.status >= 400 && error.status < 500)
          || error.code === 'stream_invalid'
          || error.code === 'stream_too_large'
        )
        if (fatalClientError) failSession(error)
        else scheduleReconnect()
      })
    }

    activeRef.current.get(key)!.resumeSubscription = connect
    connect()

    return key
  }, [abortSession, maxActive, rememberSession])

  const ensureSession = useCallback((request: SearchSessionRequest): string => {
    const normalized = normalizeSearchSessionRequest(request)
    const key = searchSessionKey(normalized)
    const cached = cacheRef.current?.read(key)
    const orphaned = cached
      && (cached.status === 'searching' || cached.status === 'resolving')
      && !activeRef.current.has(key)
    const resultLimitChanged = Boolean(
      cached && cached.request.resultLimit !== normalized.resultLimit,
    )
    if (!cached || orphaned || resultLimitChanged) {
      if (resultLimitChanged) abortSession(key, false)
      if (orphaned || resultLimitChanged) cacheRef.current?.remove(key)
      startSession(normalized)
    }
    return key
  }, [abortSession, rememberSession, startSession])

  const refreshSession = useCallback((request: SearchSessionRequest): string => {
    const key = searchSessionKey(request)
    abortSession(key, false)
    cacheRef.current?.remove(key)
    notify()
    return startSession(request, undefined, false)
  }, [abortSession, notify, startSession])

  const continueSession = useCallback((key: string, resultLimit: number): boolean => {
    const current = cacheRef.current?.read(key)
    const cleanLimit = Number(resultLimit)
    if (
      !current
      || activeRef.current.has(key)
      || !current.canContinue
      || !current.continuationToken
      || !Number.isInteger(cleanLimit)
      || cleanLimit > 999
    ) return false
    if (current.continuationMode === 'retry') {
      if (cleanLimit !== current.progress.resultLimit) return false
    } else if (current.continuationMode === 'extend') {
      if (cleanLimit <= current.progress.resultLimit) return false
    } else {
      return false
    }
    startSession(
      {
        ...current.request,
        resultLimit: cleanLimit,
        continuationToken: current.continuationToken,
      },
      current,
      false,
    )
    return true
  }, [startSession])

  const clearSessions = useCallback(async () => {
    activeRef.current.forEach((active) => {
      clearRetryTimer(active)
      active.controller.abort()
    })
    activeRef.current.clear()
    const result = await api.clearMetadataSearchSessions()
    cacheRef.current?.clear()
    clearStoredSearchRequestIds()
    notify()
    return { cleared: result.cleared, cancelled: result.cancelled }
  }, [notify])

  const restoreSession = useCallback((request: SearchSessionRequest, requestId: string) => {
    if (!/^[A-Za-z0-9_.-]{8,80}$/.test(requestId)) return false
    storeSearchRequestId(persistedSearchRequestKey(request), requestId)
    return true
  }, [])

  useEffect(() => {
    return () => {
      activeRef.current.forEach((active) => {
        clearRetryTimer(active)
        active.controller.abort()
      })
      activeRef.current.clear()
    }
  }, [])

  const value = useMemo<SearchSessionsValue>(() => ({
    revision,
    getSession,
    ensureSession,
    cancelSession,
    refreshSession,
    continueSession,
    clearSessions,
    restoreSession,
  }), [cancelSession, clearSessions, continueSession, ensureSession, getSession, refreshSession, restoreSession, revision])

  return <SearchSessionsContext.Provider value={value}>{children}</SearchSessionsContext.Provider>
}

export function useSearchSessions(): SearchSessionsValue {
  const value = useContext(SearchSessionsContext)
  if (!value) throw new Error('useSearchSessions must be used inside SearchSessionProvider')
  return value
}
