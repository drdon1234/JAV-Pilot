import type {
  AiTranslatePayload,
  AiTranslationConfigUpdate,
  AiTranslationSnapshot,
  AppSettings,
  SettingsSnapshot,
  AuthStatus,
  DownloadHistoryLookupPayload,
  RankingPayload,
  RankingPeriod,
  RankingType,
  SearchHistoryPayload,
  MediaMetadataCompletePayload,
  DownloadImportPayload,
  DownloadImportInspectPayload,
  DownloadImportPreviewPayload,
  DownloadRecoveryMode,
  DownloadReplacementPayload,
  DownloadReplacementSourceKind,
  FailedDownloadArchivePayload,
  FailedDownloadDispositionPayload,
  FailedDownloadDispositionPreviewPayload,
  DownloadRequest,
  DownloadResult,
  DetailPrefetchBatch,
  HistoryCleanupPayload,
  HistoryExportDownload,
  HistoryExportFormat,
  HistoryFilters,
  HistoryPreviewPayload,
  HistoryRetentionPolicy,
  HistoryRetentionRecoveryPayload,
  HistoryRetentionScheduleConfig,
  HistoryStatusPayload,
  HistoryVacuumPayload,
  HistoryVacuumTarget,
  MagnetProbePayload,
  MagnetSelectionPayload,
  MediaLibraryListParams,
  MediaLibraryListPayload,
  MediaLibraryActionPayload,
  MediaMetadataJob,
  MediaMetadataListPayload,
  MediaMetadataMigrationPayload,
  MediaMetadataMigrationPreviewPayload,
  MediaMetadataReviewDraftRequest,
  MediaMetadataReviewImageKind,
  MediaMetadataReviewImagePayload,
  MediaMetadataReviewPayload,
  MediaMetadataReviewPreviewPayload,
  MediaMetadataReviewPublishPayload,
  MediaMetadataReviewRefetchPayload,
  MediaMetadataReviewRefetchRequest,
  MediaMetadataScanPayload,
  NotificationConfigUpdate,
  NotificationDetailPayload,
  NotificationListPayload,
  NotificationPublicConfig,
  RuntimePayload,
  ResourceSearchActionRequest,
  ResourceSearchDownloadsRequest,
  ResourceSearchCreateRequest,
  ResourceSearchListParams,
  ResourceSearchPayload,
  ResourceSearchRemovalPayload,
  SearchRequest,
  SearchStreamEvent,
  SiteDiagnosticProbePayload,
  SiteDiagnosticsPayload,
  SiteDiagnosticSite,
  SiteSettings,
  TorrentListPayload,
  WebDownloadBatchPayload,
  WebDownloadBatchChainActionPayload,
  WebDownloadBatchChainExport,
  WebDownloadBatchChainPayload,
  WebDownloadBatchChainsPayload,
  WebDownloadBatchItemIntent,
  WebDownloadBatchRulePayload,
  WebDownloadBatchRuleSaveRequest,
  WebDownloadBatchRulesPayload,
  WebDownloadControl,
  WebDownloadActionResult,
  WebDownloadJob,
  WebDownloadListPayload,
  WebDownloadRetryFailedPayload,
  WorkResult,
} from '../types'
import { serviceErrorMessage } from './presentation'
import { METADATA_PROFILES } from './sources'

const MAX_SSE_BUFFER = 2 * 1024 * 1024
const MAX_HISTORY_EXPORT_BYTES = 64 * 1024 * 1024
export const DEFAULT_REQUEST_TIMEOUT_MS = 30_000
const LONG_REQUEST_TIMEOUT_MS = 120_000

type RequestJsonInit = RequestInit & { timeoutMs?: number }

type CoverSite = Pick<SiteSettings, 'id' | 'enabled' | 'parser_profile' | 'base_url'>

export interface PersistedMetadataSearchRequest {
  query: string
  sources: string[]
  result_limit: number
  fetch_magnets: boolean
  filters: Record<string, string>
  sort: SearchRequest['sort']
  match: SearchRequest['match']
  search_kind: NonNullable<SearchRequest['searchKind']>
  semantic_refs: Record<string, string>
  continuation_token: string | null
}

export interface PersistedMetadataSearchSession {
  request_id: string
  request: PersistedMetadataSearchRequest
  status: 'running' | 'complete' | 'cancelled' | 'error'
  terminal_event: 'done' | 'cancelled' | 'error' | null
  last_event_id: number
  created_at: number
  updated_at: number
}

const JAVBUS_DMM_IMAGE_PATH_PREFIXES = new Map<string, readonly string[]>([
  ['awsimgsrc.dmm.co.jp', ['/pics_dig/']],
  ['pics.dmm.co.jp', ['/pics_dig/', '/digital/video/']],
])
const JAVBUS_CDN_PATH_PREFIXES = ['/pics/', '/cover/', '/covers/', '/sample/', '/samples/', '/thumb/', '/thumbs/']

function decodedSafeImagePath(pathname: string): string | null {
  let decoded = pathname
  try {
    for (let attempt = 0; attempt < 6; attempt += 1) {
      const next = decodeURIComponent(decoded)
      if (next === decoded) break
      decoded = next
    }
  } catch {
    return null
  }
  if (/%[0-9a-f]{2}/i.test(decoded) || decoded.includes('\\') || decoded.includes('\0')) return null
  if (decoded.split('/').some((segment) => segment === '.' || segment === '..')) return null
  return decoded
}

function isTrustedJavBusImage(imageUrl: URL, baseUrl: URL): boolean {
  if (imageUrl.username || imageUrl.password || imageUrl.hash) return false

  const path = decodedSafeImagePath(imageUrl.pathname)
  if (!path) return false
  if (imageUrl.origin === baseUrl.origin) return path.startsWith('/pics/')
  if (imageUrl.protocol !== 'https:' || (imageUrl.port && imageUrl.port !== '443')) return false

  const baseHost = baseUrl.hostname.toLowerCase().replace(/\.$/, '')
  const targetHost = imageUrl.hostname.toLowerCase().replace(/\.$/, '')
  const siteDomain = baseHost.startsWith('www.') ? baseHost.slice(4) : baseHost
  if (siteDomain && targetHost === `pics.${siteDomain}`) {
    return JAVBUS_CDN_PATH_PREFIXES.some((prefix) => path.startsWith(prefix))
  }
  const dmmPathPrefixes = JAVBUS_DMM_IMAGE_PATH_PREFIXES.get(targetHost)
  return dmmPathPrefixes?.some((prefix) => path.startsWith(prefix)) === true
}

function isTrustedFc2Image(imageUrl: URL): boolean {
  if (
    imageUrl.protocol !== 'https:'
    || (imageUrl.port && imageUrl.port !== '443')
    || imageUrl.username
    || imageUrl.password
    || imageUrl.hash
    || imageUrl.search
  ) return false

  const path = decodedSafeImagePath(imageUrl.pathname)
  if (!path) return false

  const hostname = imageUrl.hostname.toLowerCase().replace(/\.$/, '')
  if (hostname === 'file.netcdn.space') {
    return /^\/storage\/fc2ppv\/\d{2,9}\/[^/]+\.(?:avif|jpe?g|png|webp)$/i.test(path)
      || /^\/storage\/fc2\/movies\/FC2-PPV\/\d{2,9}\/[^/]+\.(?:avif|jpe?g|png|webp)$/i.test(path)
      || /^\/ave\/vodimages\/screenshot\/(?:small|large)\/FC2-PPV-\d{2,9}\/[^/]+\.(?:avif|jpe?g|png|webp)$/i.test(path)
  }
  if (/^storage\d+\.contents\.fc2\.com$/.test(hostname)) {
    return /^\/file\/.+\.(?:avif|jpe?g|png|webp)$/i.test(path)
  }
  if (/^contents-thumbnail\d*\.fc2\.com$/.test(hostname)) {
    return /^\/w\d+\/storage\d+\.contents\.fc2\.com\/file\/.+\.(?:avif|jpe?g|png|webp)$/i.test(path)
  }
  if (hostname === 'ppvdatabank.com') {
    return /^\/article\/\d{2,9}\/img\/(?:thumb|p[sl]\d+)\.webp$/i.test(path)
  }
  return false
}

export function coverImageUrl(source: string, coverUrl: string, sites: readonly CoverSite[]): string {
  if (!coverUrl) return ''
  const site = sites.find((item) =>
    item.id === source
    && item.enabled
    && METADATA_PROFILES.some((profile) => profile === item.parser_profile),
  )
  if (!site) return coverUrl
  try {
    const imageUrl = new URL(coverUrl)
    if (site.parser_profile === 'javbus') {
      const baseUrl = new URL(site.base_url)
      if (
        !['http:', 'https:'].includes(baseUrl.protocol)
        || baseUrl.username
        || baseUrl.password
        || baseUrl.search
        || baseUrl.hash
        || !isTrustedJavBusImage(imageUrl, baseUrl)
      ) return coverUrl
    } else if (site.parser_profile === 'javdb') {
      const hostname = imageUrl.hostname.toLowerCase()
      const trustedHost = hostname === 'jdbstatic.com' || hostname.endsWith('.jdbstatic.com')
      const trustedPath = imageUrl.pathname.startsWith('/covers/') || imageUrl.pathname.startsWith('/samples/')
      if (
        imageUrl.protocol !== 'https:'
        || (imageUrl.port && imageUrl.port !== '443')
        || imageUrl.username
        || imageUrl.password
        || !trustedHost
        || !trustedPath
      ) return coverUrl
    } else if (site.parser_profile === 'fc2' && !isTrustedFc2Image(imageUrl)) {
      return coverUrl
    } else if (imageUrl.protocol !== 'https:' || imageUrl.username || imageUrl.password) {
      return ''
    }
  } catch {
    return coverUrl
  }
  return `/api/covers?source=${encodeURIComponent(site.id)}&url=${encodeURIComponent(coverUrl)}`
}

export function createSearchRequestId(): string {
  const cryptoApi = globalThis.crypto
  if (typeof cryptoApi?.randomUUID === 'function') {
    return cryptoApi.randomUUID()
  }

  if (typeof cryptoApi?.getRandomValues === 'function') {
    const bytes = cryptoApi.getRandomValues(new Uint8Array(16))
    const token = Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
    return `search-${token}`
  }

  const timestamp = Date.now().toString(36)
  const random = Math.random().toString(36).slice(2).padEnd(12, '0').slice(0, 12)
  return `search-${timestamp}-${random}`
}

export class ApiError extends Error {
  status: number
  code: string | null

  constructor(message: string, status = 0, code: string | null = null) {
    super(message)
    this.name = 'ApiError'
    this.status = status
    this.code = code
  }
}

export function retryTransientApiRequest(failureCount: number, error: unknown): boolean {
  if (failureCount >= 2) return false
  if (!(error instanceof ApiError)) return (error as { name?: string } | null)?.name !== 'AbortError'
  return error.status === 0 || error.status >= 500
}

async function requestJson<T>(path: string, init?: RequestJsonInit): Promise<T> {
  const { timeoutMs = DEFAULT_REQUEST_TIMEOUT_MS, signal: callerSignal, ...requestInit } = init ?? {}
  const controller = new AbortController()
  let timedOut = false
  const forwardAbort = () => controller.abort(callerSignal?.reason)
  if (callerSignal?.aborted) forwardAbort()
  else callerSignal?.addEventListener('abort', forwardAbort, { once: true })
  const timeout = window.setTimeout(() => {
    timedOut = true
    controller.abort(new DOMException('Request timed out', 'TimeoutError'))
  }, Math.max(1, timeoutMs))

  try {
    const response = await fetch(path, {
      credentials: 'same-origin',
      ...requestInit,
      signal: controller.signal,
      headers: {
        Accept: 'application/json',
        ...(requestInit.body ? { 'Content-Type': 'application/json' } : {}),
        ...requestInit.headers,
      },
    })
    const payload = await response.json().catch(() => ({})) as Record<string, unknown>
    if (!response.ok) {
      if (response.status === 401 && !path.startsWith('/api/auth/')) {
        window.location.assign('/login')
      }
      const message = typeof payload.error === 'string' ? payload.error : response.statusText
      const code = typeof payload.code === 'string' ? payload.code : null
      throw new ApiError(
        serviceErrorMessage({ message, code }, response.status >= 500 ? '服务暂不可用，请稍后重试' : '请求未完成，请检查输入后重试'),
        response.status,
        code,
      )
    }
    return payload as T
  } catch (error) {
    if (timedOut) throw new ApiError('请求超时，请检查网络后重试')
    if (error instanceof ApiError || callerSignal?.aborted || (error as { name?: string })?.name === 'AbortError') {
      throw error
    }
    throw new ApiError('无法连接服务，请检查网络后重试')
  } finally {
    window.clearTimeout(timeout)
    callerSignal?.removeEventListener('abort', forwardAbort)
  }
}

async function requestHistoryExport(
  filters: HistoryFilters,
  format: HistoryExportFormat,
): Promise<HistoryExportDownload> {
  const controller = new AbortController()
  let timedOut = false
  const timeout = window.setTimeout(() => {
    timedOut = true
    controller.abort(new DOMException('Request timed out', 'TimeoutError'))
  }, LONG_REQUEST_TIMEOUT_MS)
  try {
    const response = await fetch('/api/history/export', {
      method: 'POST',
      credentials: 'same-origin',
      signal: controller.signal,
      headers: {
        Accept: format === 'json' ? 'application/json' : 'text/csv',
        'Content-Type': 'application/json',
      },
      body: JSON.stringify({ filters, format }),
    })
    if (!response.ok) {
      if (response.status === 401) window.location.assign('/login')
      const payload = await response.json().catch(() => ({})) as { error?: unknown; code?: unknown }
      const message = typeof payload.error === 'string' ? payload.error : response.statusText
      const code = typeof payload.code === 'string' ? payload.code : null
      throw new ApiError(
        serviceErrorMessage(
          { message, code },
          response.status >= 500 ? '历史导出服务暂不可用，请稍后重试' : '历史导出失败，请检查筛选条件后重试',
        ),
        response.status,
        code,
      )
    }
    const rawLength = response.headers.get('Content-Length')
    const contentLength = rawLength ? Number(rawLength) : null
    if (
      contentLength !== null
      && (!Number.isSafeInteger(contentLength) || contentLength < 0 || contentLength > MAX_HISTORY_EXPORT_BYTES)
    ) {
      throw new ApiError('历史导出文件超过大小限制')
    }
    const contentType = (response.headers.get('Content-Type') || '').toLowerCase()
    const expectedType = format === 'json' ? 'application/json' : 'text/csv'
    if (!contentType.startsWith(expectedType)) throw new ApiError('历史导出响应格式无效')
    const checksumHeader = response.headers.get('X-History-Checksum') || ''
    const checksumMatch = /^(?:sha256:)?([a-f0-9]{64})$/i.exec(checksumHeader.trim())
    if (!checksumMatch) throw new ApiError('历史导出校验信息缺失')
    const blob = await response.blob()
    if (blob.size > MAX_HISTORY_EXPORT_BYTES) throw new ApiError('历史导出文件超过大小限制')
    const stamp = new Date().toISOString().slice(0, 10)
    return {
      blob,
      checksum: checksumMatch[1].toLowerCase(),
      filename: `jav-pilot-history-${stamp}.${format}`,
      format,
    }
  } catch (error) {
    if (timedOut) throw new ApiError('导出超时，请缩小筛选范围后重试')
    if (error instanceof ApiError || (error as { name?: string })?.name === 'AbortError') throw error
    throw new ApiError('无法连接服务，请检查网络后重试')
  } finally {
    window.clearTimeout(timeout)
  }
}

const WORK_RECOVERY_PARAMS = new Set([
  'q',
  'limit',
  'result_limit',
  'page',
  'page_size',
  'sort',
  'match',
  'kind',
])

export function isWorkRecoveryParam(key: string): boolean {
  return WORK_RECOVERY_PARAMS.has(key)
    || key.startsWith('filter.')
    || key.startsWith('ref.')
}

export function workRecoveryScope(workId: string, params: URLSearchParams): string {
  if (!workId.startsWith('record:')) return ''
  const entries = Array.from(params.entries())
    .filter(([key]) => isWorkRecoveryParam(key))
    .sort(([leftKey, leftValue], [rightKey, rightValue]) => (
      leftKey.localeCompare(rightKey) || leftValue.localeCompare(rightValue)
    ))
  return new URLSearchParams(entries).toString()
}

export const api = {
  authStatus: () => requestJson<AuthStatus>('/api/auth/status'),
  login: (username: string, password: string) =>
    requestJson<{ ok: boolean }>('/api/auth/login', {
      method: 'POST',
      body: JSON.stringify({ username, password }),
    }),
  logout: () => requestJson<{ ok: boolean }>('/api/auth/logout', { method: 'POST' }),
  reportClientEvent: (code: 'frontend_render_failure') =>
    requestJson<{ ok: boolean }>('/api/client-events', {
      method: 'POST',
      body: JSON.stringify({ code }),
      keepalive: true,
      timeoutMs: 5_000,
    }),
  changePassword: (payload: { username: string; current_password: string; new_password: string }) =>
    requestJson<{ ok: boolean; auth: AuthStatus }>('/api/auth/password', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  runtime: () => requestJson<RuntimePayload>('/api/config'),
  notifications: (limit = 30) => {
    const search = new URLSearchParams({ limit: String(limit) })
    return requestJson<NotificationListPayload>(`/api/notifications?${search.toString()}`)
  },
  notification: (eventId: string) => {
    const search = new URLSearchParams({ event_id: eventId })
    return requestJson<NotificationDetailPayload>(`/api/notifications?${search.toString()}`)
  },
  saveNotificationConfig: (payload: NotificationConfigUpdate) =>
    requestJson<{ ok: boolean; config: NotificationPublicConfig }>('/api/notifications/config', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  sendTestNotification: () =>
    requestJson<{ ok: boolean; event_id: string; created: boolean }>('/api/notifications/test', {
      method: 'POST',
    }),
  retryNotificationDelivery: (eventId: string, adapter?: string) =>
    requestJson<{ ok: boolean; retried: number }>('/api/notifications/retry', {
      method: 'POST',
      body: JSON.stringify({ event_id: eventId, ...(adapter ? { adapter } : {}) }),
    }),
  historyStatus: () => requestJson<HistoryStatusPayload>('/api/history/status'),
  previewHistory: (filters: HistoryFilters) =>
    requestJson<HistoryPreviewPayload>('/api/history/preview', {
      method: 'POST',
      body: JSON.stringify({ filters }),
    }),
  executeHistoryCleanup: (previewToken: string) =>
    requestJson<HistoryCleanupPayload>('/api/history/execute', {
      method: 'POST',
      body: JSON.stringify({ preview_token: previewToken }),
    }),
  updateHistoryRetention: (
    policy: HistoryRetentionPolicy,
    schedule: HistoryRetentionScheduleConfig,
  ) =>
    requestJson<HistoryStatusPayload>('/api/history/retention', {
      method: 'POST',
      body: JSON.stringify({ action: 'update', policy, schedule }),
    }),
  previewHistoryRetention: (limit: number) =>
    requestJson<HistoryPreviewPayload>('/api/history/retention', {
      method: 'POST',
      body: JSON.stringify({ action: 'preview', limit }),
    }),
  recoverHistoryRetentionState: () =>
    requestJson<HistoryRetentionRecoveryPayload>('/api/history/retention', {
      method: 'POST',
      body: JSON.stringify({ action: 'recover_state' }),
    }),
  exportHistory: (filters: HistoryFilters, format: HistoryExportFormat) =>
    requestHistoryExport(filters, format),
  vacuumHistory: (target: HistoryVacuumTarget) =>
    requestJson<HistoryVacuumPayload>('/api/history/vacuum', {
      method: 'POST',
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
      body: JSON.stringify({ target }),
    }),
  settings: () => requestJson<SettingsSnapshot>('/api/settings'),
  saveSettings: (settings: AppSettings, expectedRevision: string) =>
    requestJson<SettingsSnapshot & { ok: boolean }>('/api/settings', {
      method: 'POST',
      body: JSON.stringify({ settings, expected_revision: expectedRevision }),
    }),
  validateSettings: async (settings: AppSettings) => {
    const enteredKeys = new Map(settings.sites
      .filter((site) => site.parser_profile === 'torznab' && typeof site.torznab?.api_key === 'string')
      .map((site) => [site.id, site.torznab!.api_key!]))
    const payload = await requestJson<{ ok: boolean; settings: AppSettings }>('/api/settings/validate', {
      method: 'POST',
      body: JSON.stringify({ settings }),
    })
    // Validation responses redact secrets; retain only values entered in this draft.
    for (const site of payload.settings.sites) {
      if (site.parser_profile === 'torznab' && site.torznab && enteredKeys.has(site.id)) {
        site.torznab.api_key = enteredKeys.get(site.id)
      }
    }
    return payload.settings
  },
  siteDiagnostics: (site?: SiteDiagnosticSite) => {
    const search = new URLSearchParams()
    if (site) search.set('site', site)
    const suffix = search.size ? `?${search.toString()}` : ''
    return requestJson<SiteDiagnosticsPayload>(`/api/site-diagnostics${suffix}`)
  },
  probeSiteDiagnostics: (site: SiteDiagnosticSite, codes: { jav: string; fc2: string }) =>
    requestJson<SiteDiagnosticProbePayload>('/api/site-diagnostics/probe', {
      method: 'POST',
      body: JSON.stringify({ site, jav_code: codes.jav.trim(), fc2_code: codes.fc2.trim() }),
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
    }),
  saveQbittorrent: (payload: Record<string, unknown>) =>
    requestJson<{ ok: boolean; config: RuntimePayload['config'] }>('/api/config/qb', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  downloaderStatus: () =>
    requestJson<{ configured: boolean; ok: boolean; version?: string; error?: string }>('/api/downloader/status'),
  torrents: (filter: string, limit = 100, offset = 0, query?: string) => {
    const search = new URLSearchParams({ filter, limit: String(limit), offset: String(offset) })
    if (query) search.set('q', query)
    return requestJson<TorrentListPayload>(`/api/downloads?${search.toString()}`)
  },
  torrentAction: (action: string, hashes: string[], deleteFiles = false) =>
    requestJson<{
      ok: boolean
      metadata_jobs_removed?: number
      metadata_warning?: string
    }>('/api/downloads/action', {
      method: 'POST',
      body: JSON.stringify({ action, hashes, delete_files: deleteFiles }),
    }),
  createDownloadReplacement: (
    sourceKind: DownloadReplacementSourceKind,
    sourceId: string,
    mode: DownloadRecoveryMode,
  ) =>
    requestJson<DownloadReplacementPayload>('/api/download-replacements', {
      method: 'POST',
      body: JSON.stringify({ source_kind: sourceKind, source_id: sourceId, mode }),
    }),
  startDownloadReplacementSmartSelection: (replacementId: string) =>
    requestJson<MagnetSelectionPayload>('/api/download-replacements/smart-selection', {
      method: 'POST',
      body: JSON.stringify({ replacement_id: replacementId }),
    }),
  previewFailedDownloadDisposition: () => requestJson<FailedDownloadDispositionPreviewPayload>('/api/download-replacements/disposition-preview', {
    method: 'POST',
    body: JSON.stringify({}),
  }),
  disposeFailedDownloads: (disposition: 'archive' | 'delete', snapshotToken: string) => requestJson<FailedDownloadDispositionPayload>('/api/download-replacements/dispose', {
    method: 'POST',
    body: JSON.stringify({ disposition, snapshot_token: snapshotToken }),
    timeoutMs: LONG_REQUEST_TIMEOUT_MS,
  }),
  failedDownloadArchive: (limit = 50, offset = 0) => {
    const search = new URLSearchParams({ limit: String(limit), offset: String(offset) })
    return requestJson<FailedDownloadArchivePayload>(`/api/failed-download-archive?${search.toString()}`)
  },
  deleteFailedDownloadArchive: (code: string) => requestJson<{ ok: boolean; removed: number }>('/api/failed-download-archive/action', {
    method: 'POST',
    body: JSON.stringify({ action: 'delete', code }),
  }),
  previewDownloadImport: (input: string) =>
    requestJson<DownloadImportPreviewPayload>('/api/downloads/import/preview', {
      method: 'POST',
      body: JSON.stringify({ input }),
    }),
  inspectDownloadImport: (input: string) =>
    requestJson<DownloadImportInspectPayload>('/api/downloads/import/inspect', {
      method: 'POST',
      body: JSON.stringify({ input }),
    }),
  submitDownloadImport: (input: string, confirmUnrecognized: boolean, probeId?: string) =>
    requestJson<DownloadImportPayload>('/api/downloads/import', {
      method: 'POST',
      body: JSON.stringify({
        input,
        confirm_unrecognized: confirmUnrecognized,
        ...(probeId ? { probe_id: probeId } : {}),
      }),
    }),
  webDownloads: (params: { code?: string; query?: string; filter?: string; limit?: number; offset?: number } = {}) => {
    const search = new URLSearchParams()
    if (params.code) search.set('code', params.code)
    else {
      search.set('filter', params.filter || 'all')
      if (params.query) search.set('q', params.query)
    }
    if (params.limit) search.set('limit', String(params.limit))
    if (params.offset) search.set('offset', String(params.offset))
    return requestJson<WebDownloadListPayload>(`/api/web-downloads?${search.toString()}`)
  },
  translate: (texts: string[], target = 'zh-CN') =>
    requestJson<{ ok: boolean; translations: Array<string | null> }>('/api/translate', {
      method: 'POST',
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
      body: JSON.stringify({ texts, target }),
    }),
  aiTranslationConfig: () => requestJson<AiTranslationSnapshot>('/api/ai-translation'),
  saveAiTranslationConfig: (config: AiTranslationConfigUpdate) =>
    requestJson<AiTranslationSnapshot>('/api/ai-translation/config', {
      method: 'POST',
      body: JSON.stringify(config),
    }),
  testAiTranslation: (config?: AiTranslationConfigUpdate) =>
    requestJson<{ ok: boolean; translation: string; elapsed_ms: number }>('/api/ai-translation/test', {
      method: 'POST',
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
      body: JSON.stringify(config ? { config } : {}),
    }),
  aiTranslate: (texts: string[], target = 'zh-CN') =>
    requestJson<AiTranslatePayload>('/api/ai-translate', {
      method: 'POST',
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
      body: JSON.stringify({ texts, target }),
    }),
  searchHistory: (params: { limit?: number; offset?: number; kind?: 'metadata' | 'resource' } = {}) => {
    const search = new URLSearchParams()
    if (params.limit) search.set('limit', String(params.limit))
    if (params.offset) search.set('offset', String(params.offset))
    if (params.kind) search.set('kind', params.kind)
    return requestJson<SearchHistoryPayload>(`/api/search-history${search.size ? `?${search.toString()}` : ''}`)
  },
  searchHistoryAction: (payload: { action: 'clear' } | { action: 'remove'; id: string }) =>
    requestJson<{ ok: boolean; cleared?: number; removed?: boolean }>('/api/search-history/action', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  rankings: (period: RankingPeriod, type: RankingType, refresh = false) =>
    requestJson<RankingPayload>(`/api/rankings?period=${period}&type=${type}${refresh ? '&refresh=1' : ''}`, {
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
    }),
  downloadHistoryLookup: (codes: string[]) =>
    requestJson<DownloadHistoryLookupPayload>('/api/downloads/history-lookup', {
      method: 'POST',
      body: JSON.stringify({ codes }),
    }),
  addWebDownload: async (
    code: string,
    idempotencyKey: string,
    options: { variant?: string; replacementId?: string } = {},
  ) => {
    const payload = await requestJson<{ ok: boolean; job: WebDownloadJob }>('/api/web-downloads', {
      method: 'POST',
      body: JSON.stringify({
        code,
        idempotency_key: idempotencyKey,
        ...(options.variant ? { variant: options.variant } : {}),
        ...(options.replacementId ? { replacement_id: options.replacementId } : {}),
      }),
    })
    return payload.job
  },
  webDownloadAction: async (jobId: string, action: 'pause' | 'resume' | 'cancel' | 'retry' | 'restart' | 'remove') => {
    const payload = await requestJson<WebDownloadActionResult | { ok: boolean; job: WebDownloadActionResult }>('/api/web-downloads/action', {
      method: 'POST',
      body: JSON.stringify({ job_id: jobId, action }),
    })
    return 'job' in payload ? payload.job : payload
  },
  retryFailedWebDownloads: () =>
    requestJson<WebDownloadRetryFailedPayload>('/api/web-downloads/retry-failed', {
      method: 'POST',
      body: JSON.stringify({}),
    }),
  updateWebDownloadControl: (payload: Partial<Pick<WebDownloadControl, 'target_concurrency' | 'bandwidth_limit' | 'timezone' | 'schedule'>>) =>
    requestJson<{ ok: boolean; control: WebDownloadControl }>('/api/web-downloads/control', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  webDownloadGlobalAction: (action: 'pause' | 'resume') =>
    requestJson<{ ok: boolean; control: WebDownloadControl }>('/api/web-downloads/control', {
      method: 'POST',
      body: JSON.stringify({ action }),
    }),
  updateWebDownloadPriority: (jobId: string, priority: number, expectedRevision?: number) =>
    requestJson<{ ok: boolean; job: WebDownloadJob; control: WebDownloadControl }>('/api/web-downloads/queue', {
      method: 'POST',
      body: JSON.stringify({
        action: 'priority',
        job_id: jobId,
        priority,
        ...(expectedRevision === undefined ? {} : { expected_revision: expectedRevision }),
      }),
    }),
  reorderWebDownloads: (jobIds: string[], expectedRevision: number) =>
    requestJson<{ ok: boolean; control: WebDownloadControl }>('/api/web-downloads/queue', {
      method: 'POST',
      body: JSON.stringify({ action: 'reorder', job_ids: jobIds, expected_revision: expectedRevision }),
    }),
  cleanupMissingWebDownloads: () =>
    requestJson<{ ok: boolean; removed: number; job_ids?: string[] }>('/api/web-downloads/cleanup-missing', {
      method: 'POST',
    }),
  createResourceSearch: (payload: ResourceSearchCreateRequest) =>
    requestJson<ResourceSearchPayload>('/api/resource-searches', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  resourceSearch: ({ sessionId, limit, offset, keyword = '', variant = '' }: ResourceSearchListParams) => {
    const params = new URLSearchParams({
      id: sessionId,
      limit: String(limit),
      offset: String(offset),
    })
    if (keyword) params.set('keyword', keyword)
    if (variant) params.set('variant', variant)
    return requestJson<ResourceSearchPayload>(`/api/resource-searches?${params.toString()}`)
  },
  resourceSearchAction: (payload: ResourceSearchActionRequest) =>
    requestJson<ResourceSearchPayload | ResourceSearchRemovalPayload>('/api/resource-searches/action', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  createResourceSearchDownloads: (payload: ResourceSearchDownloadsRequest) =>
    requestJson<WebDownloadBatchPayload>('/api/resource-searches/downloads', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  webDownloadBatch: (batchId: string) =>
    requestJson<WebDownloadBatchPayload>(`/api/web-downloads/batches?id=${encodeURIComponent(batchId)}`),
  webDownloadBatchAction: (
    batchId: string,
    action: 'cancel' | 'commit' | 'continue' | 'remove' | 'retry',
    previewToken?: string,
    itemIntents?: WebDownloadBatchItemIntent[],
  ) =>
    requestJson<WebDownloadBatchPayload>('/api/web-downloads/batches/action', {
      method: 'POST',
      body: JSON.stringify({
        batch_id: batchId,
        action,
        ...((action === 'commit' || action === 'continue' || action === 'retry') && previewToken ? { preview_token: previewToken } : {}),
        ...(action === 'commit' && itemIntents ? { item_intents: itemIntents } : {}),
      }),
    }),
  webDownloadBatchChains: (params: { limit?: number; offset?: number } = {}) => {
    const search = new URLSearchParams({
      limit: String(params.limit || 20),
      offset: String(params.offset || 0),
    })
    return requestJson<WebDownloadBatchChainsPayload>(`/api/web-downloads/batches/chains?${search.toString()}`)
  },
  webDownloadBatchChain: (
    rootChainId: string,
    params: { pageLimit?: number; pageOffset?: number } = {},
  ) => {
    const search = new URLSearchParams({
      root_id: rootChainId,
      page_limit: String(params.pageLimit || 16),
      page_offset: String(params.pageOffset || 0),
    })
    return requestJson<WebDownloadBatchChainPayload>(`/api/web-downloads/batches/chains?${search.toString()}`)
  },
  cancelWebDownloadBatchChain: (rootChainId: string) =>
    requestJson<WebDownloadBatchChainActionPayload>('/api/web-downloads/batches/chains/action', {
      method: 'POST',
      body: JSON.stringify({ root_chain_id: rootChainId, action: 'cancel' }),
    }),
  exportWebDownloadBatchChain: async (rootChainId: string) => {
    const payload = await requestJson<{ ok: boolean; export: WebDownloadBatchChainExport }>(
      `/api/web-downloads/batches/chains/export?root_id=${encodeURIComponent(rootChainId)}`,
    )
    return payload.export
  },
  webDownloadBatchRules: () =>
    requestJson<WebDownloadBatchRulesPayload>('/api/web-downloads/batches/rules'),
  saveWebDownloadBatchRule: (payload: WebDownloadBatchRuleSaveRequest) =>
    requestJson<WebDownloadBatchRulePayload>('/api/web-downloads/batches/rules', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  removeWebDownloadBatchRule: (ruleId: string, expectedRevision: number) =>
    requestJson<{ ok: boolean; rule_id: string; removed: boolean; revision: number }>('/api/web-downloads/batches/rules/action', {
      method: 'POST',
      body: JSON.stringify({ rule_id: ruleId, action: 'remove', expected_revision: expectedRevision }),
    }),
  mediaLibrary: (params: MediaLibraryListParams = {}) => {
    const search = new URLSearchParams({
      limit: String(params.limit || 50),
      offset: String(params.offset || 0),
    })
    const strings: Array<[string, string | undefined]> = [
      ['q', params.query],
      ['actor', params.actor],
      ['maker', params.maker],
      ['tag', params.tag],
      ['series', params.series],
      ['source', params.source],
      ['presence', params.presence],
      ['completeness', params.completeness],
      ['anomaly', params.anomaly],
    ]
    strings.forEach(([key, value]) => {
      if (value) search.set(key, value)
    })
    if (params.min_height !== undefined) search.set('min_height', String(params.min_height))
    if (params.max_height !== undefined) search.set('max_height', String(params.max_height))
    return requestJson<MediaLibraryListPayload>(`/api/library?${search.toString()}`)
  },
  rebuildMediaLibrary: (expectedRevision: number, acceptRootChange = false) =>
    requestJson<MediaLibraryActionPayload>('/api/library/action', {
      method: 'POST',
      body: JSON.stringify({
        action: acceptRootChange ? 'accept_root_change' : 'rebuild',
        expected_revision: expectedRevision,
      }),
    }),
  mediaMetadata: (params: { filter?: string; query?: string; limit?: number; offset?: number } = {}) => {
    const search = new URLSearchParams({
      filter: params.filter || 'all',
      limit: String(params.limit || 50),
      offset: String(params.offset || 0),
    })
    if (params.query) search.set('q', params.query)
    return requestJson<MediaMetadataListPayload>(`/api/media-metadata?${search.toString()}`)
  },
  scanMediaMetadata: (code?: string) =>
    requestJson<MediaMetadataScanPayload>('/api/media-metadata/scan', {
      method: 'POST',
      body: JSON.stringify(code ? { code } : {}),
    }),
  previewMediaMetadataTitles: (code?: string) =>
    requestJson<MediaMetadataMigrationPreviewPayload>('/api/media-metadata/migrate-titles', {
      method: 'POST',
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
      body: JSON.stringify({
        action: 'preview',
        ...(code !== undefined ? { code } : {}),
      }),
    }),
  migrateMediaMetadataTitles: (previewId: string) =>
    requestJson<MediaMetadataMigrationPayload>('/api/media-metadata/migrate-titles', {
      method: 'POST',
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
      body: JSON.stringify({ action: 'migrate', preview_id: previewId }),
    }),
  completeMediaMetadata: () =>
    requestJson<MediaMetadataCompletePayload>('/api/media-metadata/action', {
      method: 'POST',
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
      body: JSON.stringify({ action: 'complete_all' }),
    }),
  retryMediaMetadata: (jobId: string) =>
    requestJson<{ ok: boolean; job?: MediaMetadataJob }>('/api/media-metadata/action', {
      method: 'POST',
      body: JSON.stringify({ job_id: jobId, action: 'retry' }),
    }),
  openMediaMetadataReview: (code: string, relativeMediaPath: string) =>
    requestJson<MediaMetadataReviewPayload>('/api/media-metadata/review/open', {
      method: 'POST',
      body: JSON.stringify({ code, relative_media_path: relativeMediaPath }),
    }),
  mediaMetadataReview: (reviewId: string) =>
    requestJson<MediaMetadataReviewPayload>(`/api/media-metadata/review?id=${encodeURIComponent(reviewId)}`),
  updateMediaMetadataReviewDraft: (payload: MediaMetadataReviewDraftRequest) =>
    requestJson<MediaMetadataReviewPayload>('/api/media-metadata/review/draft', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  abandonMediaMetadataReview: (reviewId: string, expectedRevision: number) =>
    requestJson<MediaMetadataReviewPayload>('/api/media-metadata/review/abandon', {
      method: 'POST',
      body: JSON.stringify({
        review_id: reviewId,
        expected_revision: expectedRevision,
      }),
    }),
  refetchMediaMetadataReview: (payload: MediaMetadataReviewRefetchRequest) =>
    requestJson<MediaMetadataReviewRefetchPayload>('/api/media-metadata/review/refetch', {
      method: 'POST',
      timeoutMs: LONG_REQUEST_TIMEOUT_MS,
      body: JSON.stringify(payload),
    }),
  uploadMediaMetadataReviewImage: (
    reviewId: string,
    kind: MediaMetadataReviewImageKind,
    bodyBase64: string,
    expectedRevision: number,
  ) => requestJson<MediaMetadataReviewImagePayload>('/api/media-metadata/review/image', {
    method: 'POST',
    timeoutMs: LONG_REQUEST_TIMEOUT_MS,
    body: JSON.stringify({
      review_id: reviewId,
      kind,
      body_base64: bodyBase64,
      expected_revision: expectedRevision,
    }),
  }),
  previewMediaMetadataReview: (payload: {
    review_id: string
    include_nfo: boolean
    portrait_ref?: string
    landscape_ref?: string
    expected_revision: number
  }) => requestJson<MediaMetadataReviewPreviewPayload>('/api/media-metadata/review/preview', {
    method: 'POST',
    body: JSON.stringify(payload),
  }),
  publishMediaMetadataReview: (previewToken: string) =>
    requestJson<MediaMetadataReviewPublishPayload>('/api/media-metadata/review/publish', {
      method: 'POST',
      body: JSON.stringify({ preview_token: previewToken }),
    }),
  addDownload: (payload: DownloadRequest) =>
    requestJson<DownloadResult>('/api/download', {
      method: 'POST',
      body: JSON.stringify(payload),
    }),
  startMagnetProbe: (magnets: string[]) =>
    requestJson<MagnetProbePayload>('/api/magnets/probe', {
      method: 'POST',
      body: JSON.stringify({ magnets }),
    }),
  magnetProbe: (probeId: string) =>
    requestJson<MagnetProbePayload>(`/api/magnets/probe?probe_id=${encodeURIComponent(probeId)}`),
  startMagnetSelection: (magnets: string[]) =>
    requestJson<MagnetSelectionPayload>('/api/magnets/select', {
      method: 'POST',
      body: JSON.stringify({ magnets }),
    }),
  magnetSelection: (selectionId: string) =>
    requestJson<MagnetSelectionPayload>(`/api/magnets/select?selection_id=${encodeURIComponent(selectionId)}`),
  organizerPreview: (payload: Record<string, unknown>) =>
    requestJson<{
      ok: boolean
      matched: boolean
      rule: { id: string; name: string } | null
      destination: { category: string; save_path: string; tags: string } | null
    }>(
      '/api/organizer/preview',
      { method: 'POST', body: JSON.stringify(payload) },
    ),
  work: async (workId: string, routeParams?: URLSearchParams, signal?: AbortSignal) => {
    const params = new URLSearchParams({ work_id: workId })
    routeParams?.forEach((value, key) => {
      if (key === 'code' || key === 'source' || (workId.startsWith('record:') && isWorkRecoveryParam(key))) {
        params.set(key, value)
      }
    })
    const payload = await requestJson<WorkResult | { work: WorkResult }>(
      `/api/works?${params.toString()}`,
      { signal },
    )
    return 'work' in payload ? payload.work : payload
  },
  createDetailPrefetchBatch: async (requestId: string, workIds: string[], source = 'all') => {
    const payload = await requestJson<{ ok: boolean; batch: DetailPrefetchBatch }>(
      '/api/detail-prefetch/batches',
      {
        method: 'POST',
        body: JSON.stringify({
          request_id: requestId,
          work_ids: workIds,
          source_scope: source,
        }),
      },
    )
    return payload.batch
  },
  detailPrefetchBatch: async (batchId: string) => {
    const payload = await requestJson<{ ok: boolean; batch: DetailPrefetchBatch }>(
      `/api/detail-prefetch/batches/${encodeURIComponent(batchId)}`,
    )
    return payload.batch
  },
  cancelDetailPrefetchBatch: async (batchId: string) => {
    const payload = await requestJson<{ ok: boolean; batch: DetailPrefetchBatch }>(
      '/api/detail-prefetch/batches/action',
      {
        method: 'POST',
        body: JSON.stringify({ action: 'cancel', batch_id: batchId }),
      },
    )
    return payload.batch
  },
  cancelSearch: (requestId: string) =>
    requestJson<{ ok: boolean; cancelled: boolean }>('/api/search/cancel', {
      method: 'POST',
      keepalive: true,
      body: JSON.stringify({ request_id: requestId }),
    }),
  latestMetadataSearchSession: async () => {
    const payload = await requestJson<{
      ok: boolean
      session: PersistedMetadataSearchSession | null
    }>('/api/search/sessions')
    return payload.session
  },
  metadataSearchSession: async (requestId: string, signal?: AbortSignal) => {
    const params = new URLSearchParams({ request_id: requestId, optional: '1' })
    const payload = await requestJson<{
      ok: boolean
      session: PersistedMetadataSearchSession | null
    }>(`/api/search/sessions?${params.toString()}`, { signal })
    return payload.session
  },
  clearMetadataSearchSessions: () =>
    requestJson<{ ok: boolean; cleared: number; cancelled: number }>(
      '/api/search/sessions/action',
      { method: 'POST', body: JSON.stringify({ action: 'clear' }) },
    ),
}

export async function streamSearch(
  request: SearchRequest,
  signal: AbortSignal,
  onEvent: (event: SearchStreamEvent) => void,
  subscription: {
    afterEventId?: number
    onCursor?: (eventId: number) => void
  } = {},
): Promise<void> {
  const existing = await api.metadataSearchSession(request.requestId, signal)
  if (!existing) {
    await requestJson('/api/search/sessions', {
      method: 'POST',
      signal,
      body: JSON.stringify({
        request_id: request.requestId,
        query: request.query,
        sources: request.sources,
        result_limit: request.resultLimit,
        fetch_magnets: request.fetchMagnets,
        filters: request.filters,
        sort: request.sort,
        match: request.match,
        search_kind: request.searchKind ?? 'keyword',
        semantic_refs: request.semanticRefs ?? {},
        continuation_token: request.continuationToken ?? null,
      }),
    })
  }

  const afterEventId = Number.isInteger(subscription.afterEventId) && Number(subscription.afterEventId) >= 0
    ? Number(subscription.afterEventId)
    : 0
  const params = new URLSearchParams({
    request_id: request.requestId,
    after: String(afterEventId),
  })
  const response = await fetch(`/api/search/sessions/stream?${params.toString()}`, {
    signal,
    credentials: 'same-origin',
    headers: {
      Accept: 'text/event-stream',
    },
  })
  if (!response.ok) {
    if (response.status === 401) window.location.assign('/login')
    const payload = await response.json().catch(() => ({})) as { error?: unknown; code?: unknown }
    const message = typeof payload.error === 'string' ? payload.error : response.statusText
    const code = typeof payload.code === 'string' ? payload.code : null
    throw new ApiError(
      serviceErrorMessage(
        { message, code },
        response.status >= 500 ? '搜索服务暂不可用，请稍后重试' : '搜索请求未完成，请检查条件后重试',
      ),
      response.status,
      code,
    )
  }
  if (!response.body) {
    throw new ApiError('浏览器不支持流式响应')
  }

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let terminalReceived = false
  const dispatch = (event: SearchStreamEvent | null) => {
    if (!event) return
    const cursor = Number((event.payload as unknown as { event_cursor?: unknown }).event_cursor)
    if (Number.isInteger(cursor) && cursor > 0) subscription.onCursor?.(cursor)
    if (event.event === 'done' || event.event === 'cancelled' || event.event === 'error') {
      terminalReceived = true
    }
    onEvent(event)
  }
  try {
    while (true) {
      const chunk = await reader.read()
      if (chunk.done) break
      buffer += decoder.decode(chunk.value, { stream: true })
      if (buffer.length > MAX_SSE_BUFFER) {
        throw new ApiError('搜索流响应过大', 0, 'stream_too_large')
      }
      const blocks = buffer.split('\n\n')
      buffer = blocks.pop() ?? ''
      blocks.forEach((block) => {
        dispatch(parseSseBlock(block))
      })
    }
    buffer += decoder.decode()
    if (buffer.trim()) {
      dispatch(parseSseBlock(buffer))
    }
    if (!terminalReceived) throw new ApiError('搜索连接提前结束，已接收的结果已保留，请重试')
  } finally {
    if (!terminalReceived) await reader.cancel().catch(() => undefined)
    reader.releaseLock()
  }
}

export function parseSseBlock(block: string): SearchStreamEvent | null {
  let eventName = ''
  const data: string[] = []
  block.split(/\r?\n/).forEach((line) => {
    if (line.startsWith('event:')) eventName = line.slice(6).trim()
    if (line.startsWith('data:')) data.push(line.slice(5).trimStart())
  })
  if (!eventName || !data.length) return null
  if (!['source', 'delta', 'base', 'result', 'done', 'cancelled', 'error'].includes(eventName)) return null
  try {
    return { event: eventName, payload: JSON.parse(data.join('\n')) } as SearchStreamEvent
  } catch {
    throw new ApiError('搜索流包含无效数据', 0, 'stream_invalid')
  }
}
