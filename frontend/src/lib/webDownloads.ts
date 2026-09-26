import type { WebDownloadBatchStatus, WebDownloadJob, WebDownloadListPayload, WebDownloadVariant } from '../types'

export const TERMINAL_WEB_DOWNLOAD_STATUSES = new Set(['completed', 'failed', 'cancelled'])
const WEB_DOWNLOAD_RESELECTION_ACTIVE_STATUSES = new Set([
  'queued',
  'retry_wait',
  'locating',
  'validating',
  'downloading',
  'verifying',
  'archiving',
  'pausing',
  'paused',
  'cancelling',
])
export const WEB_DOWNLOAD_HISTORY_QUERY_ROOT = ['web-downloads', 'history'] as const

export const webDownloadBatchStatusLabels: Record<WebDownloadBatchStatus, string> = {
  queued: '等待发现',
  discovering: '正在发现',
  ready: '等待确认',
  too_many: '范围过大',
  incomplete: '发现不完整',
  failed: '发现失败',
  cancelled: '已取消',
  expired: '预览已过期',
  committed: '已加入队列',
}

export function webDownloadHistoryQueryKey(filter: string, query: string, page: number) {
  return [...WEB_DOWNLOAD_HISTORY_QUERY_ROOT, filter, query, page] as const
}

type HistoryEntity = { created_at: number | string }

function historyTimestamp(value: number | string): number {
  if (typeof value === 'number') return value < 10_000_000_000 ? value * 1_000 : value
  const parsed = Date.parse(value)
  return Number.isFinite(parsed) ? parsed : 0
}

function compareHistoryEntities<T extends HistoryEntity>(
  left: T,
  right: T,
  identity: (entity: T) => string,
): number {
  const timeOrder = historyTimestamp(right.created_at) - historyTimestamp(left.created_at)
  if (timeOrder) return timeOrder
  const leftId = identity(left)
  const rightId = identity(right)
  return leftId === rightId ? 0 : leftId > rightId ? -1 : 1
}

function firstPageMissesObservedEntity<T extends HistoryEntity>(
  current: readonly T[],
  observed: readonly T[],
  identity: (entity: T) => string,
  currentPageIsComplete: boolean,
): boolean {
  const currentIds = new Set(current.map(identity))
  const missing = observed.filter((entity) => !currentIds.has(identity(entity)))
  if (!missing.length) return false
  if (currentPageIsComplete || !current.length) return true

  const boundary = [...current].sort((left, right) => compareHistoryEntities(left, right, identity)).at(-1)!
  return missing.some((entity) => compareHistoryEntities(entity, boundary, identity) <= 0)
}

export function webDownloadAllHistoryIsStale(
  current: WebDownloadListPayload,
  observed: WebDownloadListPayload,
): boolean {
  const observedSummary = observed.summary
  const currentSummary = current.summary
  if (observedSummary) {
    if (!currentSummary && current.count !== observedSummary.total) return true
    if (currentSummary && (
      currentSummary.total !== observedSummary.total
      || currentSummary.running !== observedSummary.running
      || currentSummary.queued !== observedSummary.queued
      || (currentSummary.retrying ?? 0) !== (observedSummary.retrying ?? 0)
      || currentSummary.completed !== observedSummary.completed
      || currentSummary.missing !== observedSummary.missing
      || currentSummary.failed !== observedSummary.failed
    )) return true
  }

  if (firstPageMissesObservedEntity(
    current.tasks ?? [],
    observed.tasks ?? [],
    (job) => job.job_id,
    current.has_more !== true,
  )) return true

  const currentIntents = current.intents ?? []
  return firstPageMissesObservedEntity(
    currentIntents,
    observed.intents ?? [],
    (intent) => intent.batch_id,
    currentIntents.length < 100,
  )
}

const STATUS_LABELS: Record<string, string> = {
  queued: '排队中',
  retry_wait: '等待恢复',
  locating: '定位片源',
  validating: '校验片源',
  downloading: '下载中',
  verifying: '验证文件',
  archiving: '归档中',
  completed: '已完成',
  failed: '失败',
  pausing: '正在暂停',
  paused: '已暂停',
  cancelling: '正在取消',
  cancelled: '已取消',
}

const VARIANT_LABELS: Record<WebDownloadVariant, string> = {
  original: '原片',
  chinese_subtitle: '中文字幕',
  uncensored_leak: '无码影片',
}

export const WEB_DOWNLOAD_VARIANTS: readonly WebDownloadVariant[] = [
  'original',
  'chinese_subtitle',
  'uncensored_leak',
]

export function createWebDownloadIntentKey(): string {
  const bytes = new Uint8Array(16)
  if (typeof globalThis.crypto?.getRandomValues === 'function') {
    globalThis.crypto.getRandomValues(bytes)
    return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
  }
  for (let index = 0; index < bytes.length; index += 1) {
    bytes[index] = Math.floor(Math.random() * 256)
  }
  return Array.from(bytes, (value) => value.toString(16).padStart(2, '0')).join('')
}

export function webDownloadVariantLabel(variant: WebDownloadVariant): string {
  return VARIANT_LABELS[variant]
}

export function webDownloadStatusLabel(status: string): string {
  return STATUS_LABELS[status] || status || '未知状态'
}

export function webDownloadStatusTone(status: string) {
  if (status === 'completed') return 'success' as const
  if (status === 'failed') return 'danger' as const
  if (status === 'retry_wait' || status === 'cancelled' || status === 'cancelling' || status === 'pausing') return 'warning' as const
  if (status === 'locating' || status === 'validating' || status === 'downloading' || status === 'verifying' || status === 'archiving') return 'info' as const
  return 'neutral' as const
}

export function webDownloadProgress(job: WebDownloadJob): number {
  if (job.status === 'completed') return 1
  const value = Number(job.progress)
  if (!Number.isFinite(value) || value <= 0) return 0
  return Math.min(1, value / 100)
}

export function webDownloadIsActive(job: WebDownloadJob): boolean {
  return !TERMINAL_WEB_DOWNLOAD_STATUSES.has(job.status)
}

export function webDownloadCanReselect(job: Pick<WebDownloadJob, 'status' | 'error'>): boolean {
  if (WEB_DOWNLOAD_RESELECTION_ACTIVE_STATUSES.has(job.status)) return false
  return job.status === 'failed' || job.status === 'cancelled' || Boolean(job.error?.trim())
}

export function webDownloadOccupiesSlot(job: WebDownloadJob): boolean {
  return ['locating', 'validating', 'downloading', 'verifying', 'archiving', 'cancelling', 'pausing'].includes(job.status)
}

export function webDownloadContinueHint(job: WebDownloadJob): string | null {
  if (!job.can_retry || (job.status !== 'failed' && job.status !== 'cancelled')) return null
  return '继续下载时，若有可用检查点将复用。'
}

export function webDownloadRetryStatus(
  job: WebDownloadJob,
  nowSeconds = Date.now() / 1_000,
): string | null {
  if (job.status !== 'retry_wait') return null
  const retryCount = Number(job.retry_count)
  const attempt = Number.isInteger(retryCount) && retryCount > 0
    ? Math.min(3, retryCount)
    : null
  const retryAt = Number(job.next_retry_at)
  const prefix = attempt ? `第 ${attempt} / 3 次自动恢复` : '等待自动恢复'
  if (!Number.isFinite(retryAt) || retryAt <= 0) return prefix
  if (retryAt <= nowSeconds) return `${prefix}，已到恢复时间，等待调度`
  return `${prefix}，最早于 ${formatWebDownloadDateTime(retryAt)} 尝试恢复`
}

export function webDownloadPollInterval(
  job: WebDownloadJob,
  activeIntervalMs: number,
  nowSeconds = Date.now() / 1_000,
): number | false {
  if (!webDownloadIsActive(job)) return false
  const activeInterval = Number.isFinite(activeIntervalMs) && activeIntervalMs > 0
    ? activeIntervalMs
    : 2_000
  if (job.status === 'paused') return Math.max(activeInterval, 10_000)
  if (job.status !== 'retry_wait') return activeInterval
  const retryAt = Number(job.next_retry_at)
  if (!Number.isFinite(retryAt) || retryAt <= 0) return Math.max(activeInterval, 10_000)
  const remainingSeconds = retryAt - nowSeconds
  if (remainingSeconds <= 10) return activeInterval
  if (remainingSeconds <= 60) return Math.max(activeInterval, 10_000)
  return Math.max(activeInterval, 30_000)
}

export function webDownloadQualityLabel(job: WebDownloadJob): string {
  if (job.publication_outcome === 'kept_existing') return '保留已有画质'
  const selectedHeight = Number(job.selected_height)
  if (Number.isInteger(selectedHeight) && selectedHeight > 0) return `${selectedHeight}p`
  const requestedHeight = Number(job.requested_height)
  if (job.quality_strategy === 'highest') {
    if (!Number.isInteger(requestedHeight) || requestedHeight <= 0) return '最高可用画质'
    const ceiling = requestedHeight === 4320 ? '8K' : requestedHeight === 2160 ? '4K' : `${requestedHeight}p`
    return `最高可用 · 上限 ${ceiling}`
  }
  if (Number.isInteger(requestedHeight) && requestedHeight > 0) return `${requestedHeight}p`
  return '历史自动画质'
}

function jobUpdatedAt(job: WebDownloadJob): number {
  if (typeof job.updated_at === 'number') return job.updated_at < 10_000_000_000 ? job.updated_at * 1000 : job.updated_at
  const parsed = Date.parse(job.updated_at)
  return Number.isFinite(parsed) ? parsed : 0
}

export function webDownloadDispatchOrder(tasks: readonly WebDownloadJob[]): WebDownloadJob[] {
  // Mirrors the backend scheduler ordering (priority DESC, queue_position,
  // created_at, job_id); reorder submissions are interpreted by the server in
  // this sequence, not in the created_at DESC order the list endpoint returns.
  return [...tasks].sort((left, right) => {
    const priorityOrder = (right.priority ?? 0) - (left.priority ?? 0)
    if (priorityOrder) return priorityOrder
    const positionOrder = (left.queue_position ?? 0) - (right.queue_position ?? 0)
    if (positionOrder) return positionOrder
    const createdOrder = historyTimestamp(left.created_at) - historyTimestamp(right.created_at)
    if (createdOrder) return createdOrder
    return left.job_id === right.job_id ? 0 : left.job_id > right.job_id ? 1 : -1
  })
}

export function webDownloadDisplayTasks(tasks: readonly WebDownloadJob[]): WebDownloadJob[] {
  // Queued rows render in dispatch order inside the row slots they already
  // occupy, so moving a task visibly moves its row; other statuses keep the
  // server-provided history order.
  const queued = webDownloadDispatchOrder(tasks.filter((job) => job.status === 'queued'))
  if (queued.length < 2) return [...tasks]
  let slot = 0
  return tasks.map((job) => (job.status === 'queued' ? queued[slot++] : job))
}

export function currentWebDownloadJob(tasks: readonly WebDownloadJob[]): WebDownloadJob | null {
  return [...tasks].sort((left, right) => {
    const activeOrder = Number(webDownloadIsActive(right)) - Number(webDownloadIsActive(left))
    return activeOrder || jobUpdatedAt(right) - jobUpdatedAt(left)
  })[0] ?? null
}

export function formatWebDownloadDateTime(value: number | string): string {
  if (value === '' || value === 0) return '未知'
  const date = typeof value === 'number'
    ? new Date(value < 10_000_000_000 ? value * 1000 : value)
    : new Date(value)
  if (!Number.isFinite(date.getTime())) return '未知'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(date)
}
