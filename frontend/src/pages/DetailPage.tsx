import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ChevronLeft, ChevronRight, Download, ExternalLink, LoaderCircle, RadioTower, RotateCcw, WandSparkles, X } from 'lucide-react'
import { type KeyboardEvent, memo, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useParams, useSearchParams } from 'react-router-dom'

import { useToast } from '../components/ToastProvider'
import { Button, EmptyState, IconButton, InlineNotice, PageHeader, ProgressBar, SkeletonRows, StatusBadge } from '../components/ui'
import { api, coverImageUrl, workRecoveryScope } from '../lib/api'
import { formatBytes, formatEta } from '../lib/format'
import { downloadHistorySummary } from '../lib/downloadHistory'
import { useTranslationPreferences, useTranslations } from '../lib/translation'
import { useAiTranslations } from '../lib/aiTranslation'
import { AiTranslateButton, AiTranslationLine } from '../components/AiTranslation'
import { parseStatusLabel, serviceErrorMessage } from '../lib/presentation'
import { METADATA_PROFILES } from '../lib/sources'
import { createWebDownloadIntentKey, currentWebDownloadJob, WEB_DOWNLOAD_HISTORY_QUERY_ROOT, webDownloadCanReselect, webDownloadContinueHint, webDownloadIsActive, webDownloadPollInterval, webDownloadProgress, webDownloadQualityLabel, webDownloadRetryStatus, webDownloadStatusLabel, webDownloadStatusTone, webDownloadVariantLabel } from '../lib/webDownloads'
import type { DownloadReselection, MagnetProbePayload, MagnetSelectionPayload, RelatedRef, SiteSettings, WebDownloadBatch, WebDownloadJob, WebDownloadListPayload, WorkImage, WorkMagnet, WorkResult, WorkSource, WorkSourceDetails } from '../types'
import { canonicalMagnetAction, sourceName, StableImage, type DownloadState, uniqueSampleImages, WorkMagnetList, workBackdrop, workCover } from './WorkUi'
import '../styles/detail.css'

type ImageLoadState = 'loaded' | 'failed'
const TERMINAL_PROBE_STATUSES = new Set(['complete', 'failed', 'cancelled'])
const TERMINAL_SELECTION_STATUSES = new Set(['complete', 'failed', 'cancelled'])
const ACTIVE_WEB_DOWNLOAD_INTENT_STATUSES = new Set(['queued', 'discovering', 'ready'])
const WEB_DOWNLOAD_INTENT_BRIDGE_MS = 15_000

function canonicalSourceScope(value: string): string {
  return Array.from(new Set((value || 'all').split(',').filter(Boolean))).sort().join(',')
}

const RELATED_GROUPS: Array<{ key: keyof WorkSourceDetails; label: string }> = [
  { key: 'makers', label: '制作商' },
  { key: 'publishers', label: '发行商' },
  { key: 'series', label: '系列' },
  { key: 'directors', label: '导演' },
  { key: 'actors', label: '演员' },
  { key: 'tags', label: '标签' },
]
const RELATED_KEY_BY_KIND: Partial<Record<RelatedRef['kind'], keyof WorkSourceDetails>> = {
  maker: 'makers',
  publisher: 'publishers',
  series: 'series',
  director: 'directors',
  actor: 'actors',
  tag: 'tags',
}
const METADATA_PARSER_PROFILES = new Set<string>(METADATA_PROFILES)

function hasSourceError(work: WorkResult | undefined): boolean {
  return Boolean(work?.sources.some((source) => Boolean(
    source.error || source.details_error || source.image_error || source.magnet_error
  )))
}

function relatedLabelsMatch(left: RelatedRef, right: RelatedRef): boolean {
  return left.kind === right.kind
    && left.label.trim().toLocaleLowerCase() === right.label.trim().toLocaleLowerCase()
}

function normalizeRelatedSearchReference(value: string, site: SiteSettings | undefined): string | null {
  if (!site?.base_url || !value.trim()) return null
  try {
    const base = new URL(site.base_url)
    const reference = new URL(value, base)
    if (reference.origin !== base.origin || !reference.pathname.startsWith('/')) return null
    for (const key of reference.searchParams.keys()) {
      if (/authorization|cookie|header|manifest|password|referer|secret|session|token|url/i.test(key)) return null
    }
    return `${reference.pathname}${reference.search}`
  } catch {
    return null
  }
}

function hasSourceDetails(details: WorkSourceDetails | undefined): details is WorkSourceDetails {
  if (!details) return false
  return Boolean(
    details.duration_minutes
    || details.duration_text
    || details.rating
    || RELATED_GROUPS.some(({ key }) => Array.isArray(details[key]) && (details[key] as RelatedRef[]).length),
  )
}

function SourceDetailsView({
  details,
  hrefFor,
  fieldSources,
}: {
  details: WorkSourceDetails
  hrefFor: (item: RelatedRef) => string
  fieldSources?: WorkSource['field_sources']
}) {
  const relationGroups = RELATED_GROUPS.flatMap(({ key, label }) => {
    const values = details[key]
    return Array.isArray(values) && values.length ? [{ key, label, values: values as RelatedRef[] }] : []
  })
  const duration = details.duration_text || (details.duration_minutes ? `${details.duration_minutes} 分钟` : '')

  return (
    <div className="source-details">
      {duration || details.rating ? (
        <dl className="source-detail-facts">
          {duration ? <div><dt>时长</dt><dd>{duration}</dd></div> : null}
          {details.rating ? (
            <div>
              <dt>评分</dt>
              <dd>
                {details.rating.value !== null ? <strong>{details.rating.value}</strong> : <span>暂无分数</span>}
                {details.rating.votes !== null ? <span>{details.rating.votes} 人评价</span> : null}
              </dd>
            </div>
          ) : null}
        </dl>
      ) : null}
      {relationGroups.length ? (
        <dl className="source-relations">
          {relationGroups.map(({ key, label, values }) => (
            <div key={String(key)}>
              <dt>{label}</dt>
              <dd>
                {values.map((item) => (
                  <Link className="related-search-link" to={hrefFor(item)} key={`${item.kind}-${item.label}-${item.url || ''}`}>
                    {item.label}
                  </Link>
                ))}
              </dd>
            </div>
          ))}
        </dl>
      ) : null}
      {fieldSources && Object.keys(fieldSources).length ? (
        <details className="source-provenance">
          <summary>字段来源</summary>
          <dl className="source-detail-facts">
            {Object.entries(fieldSources).map(([field, source]) => (
              <div key={field}>
                <dt>{({ title: '标题', code: '番号', release_date: '发行日期', duration_minutes: '时长',
                  duration_text: '时长', makers: '片商', publishers: '发行商', series: '系列', directors: '导演',
                  actors: '演员', tags: '标签', rating: '评分', images: '图片', description: '简介',
                  original_title: '原始标题' } as Record<string, string>)[field] ?? field}</dt>
                <dd>
                  {/^https?:\/\//i.test(source.url)
                    ? <a href={source.url} target="_blank" rel="noreferrer">{source.provider ?? source.source_id}</a>
                    : source.provider ?? source.source_id}
                  {source.upstream_source ? <span> · 上游 {source.upstream_url && /^https?:\/\//i.test(source.upstream_url)
                    ? <a href={source.upstream_url} target="_blank" rel="noreferrer">{source.upstream_source}</a>
                    : source.upstream_source}</span> : null}
                  {source.contributors?.map((contributor, index) => <span key={`${contributor.provider}-${index}`}>
                    {' · '}{/^https?:\/\//i.test(contributor.url)
                      ? <a href={contributor.url} target="_blank" rel="noreferrer">{contributor.provider ?? contributor.source_id}</a>
                      : contributor.provider ?? contributor.source_id}
                  </span>)}
                </dd>
              </div>
            ))}
          </dl>
        </details>
      ) : null}
    </div>
  )
}

const GalleryThumbnail = memo(function GalleryThumbnail({
  sourceId,
  sourceLabel,
  image,
  index,
  sites,
  retryToken,
  onImageState,
  onOpen,
}: {
  sourceId: string
  sourceLabel: string
  image: WorkImage
  index: number
  sites: readonly SiteSettings[]
  retryToken: number
  onImageState: (sourceId: string, key: string, state: ImageLoadState) => void
  onOpen: (index: number) => void
}) {
  const alt = `${sourceLabel} 截图 ${index + 1}`
  const thumbnailUrl = coverImageUrl(sourceId, image.url, sites)
  return (
    <button
      type="button"
      className="gallery-item gallery-item-sample"
      aria-label={`查看 ${alt}`}
      onClick={() => onOpen(index)}
    >
      <StableImage
        src={thumbnailUrl}
        alt={alt}
        retryToken={retryToken}
        referrerPolicy="no-referrer"
        onLoad={() => onImageState(sourceId, image.url, 'loaded')}
        onError={() => onImageState(sourceId, image.url, 'failed')}
      />
    </button>
  )
})

export const WorkMediaGallery = memo(function WorkMediaGallery({
  sourceId,
  sourceLabel,
  images,
  sites,
  retryToken,
  imageStates,
  onImageState,
  onOpen,
}: {
  sourceId: string
  sourceLabel: string
  images: readonly WorkImage[]
  sites: readonly SiteSettings[]
  retryToken?: number
  imageStates: Readonly<Record<string, ImageLoadState>>
  onImageState: (sourceId: string, key: string, state: ImageLoadState) => void
  onOpen: (index: number) => void
}) {
  if (!images.length) return <EmptyState className="compact-empty" title="该站点暂无截图" />

  const loadedImageCount = images.filter((image) => imageStates[image.url] === 'loaded').length
  const failedImageCount = images.filter((image) => imageStates[image.url] === 'failed').length
  const settledImageCount = loadedImageCount + failedImageCount

  return (
    <>
      <div className="gallery-progress" aria-live="polite">
        <div>
          <span>已处理 {settledImageCount} / {images.length}</span>
          {failedImageCount ? <span className="gallery-failure-count">{failedImageCount} 张失败</span> : null}
        </div>
        <ProgressBar
          value={settledImageCount / images.length}
          label={`${sourceLabel} 截图加载进度`}
        />
      </div>
      <div className="source-gallery">
        {images.map((image, index) => (
          <GalleryThumbnail
            sourceId={sourceId}
            sourceLabel={sourceLabel}
            image={image}
            index={index}
            sites={sites}
            retryToken={retryToken ?? 0}
            onImageState={onImageState}
            onOpen={onOpen}
            key={image.url}
          />
        ))}
      </div>
    </>
  )
})

function probeAnnouncement(probe: MagnetProbePayload, availableCount: number): string {
  if (probe.status === 'queued') return '做种探测已排队'
  if (probe.status === 'running') return availableCount ? '已发现有做种资源' : '正在探测做种'
  if (probe.status === 'cleaning') return '观察结束，正在清理临时任务'
  if (probe.status === 'cancelling') return '正在停止做种探测'
  if (probe.status === 'complete') return availableCount
    ? `做种探测已完成，发现 ${availableCount} 条有做种`
    : '做种探测已完成'
  if (probe.status === 'failed') return '做种探测失败'
  return '做种探测已取消'
}

function probeProgressValue(probe: MagnetProbePayload): number {
  if (probe.status === 'complete') return 1
  if (!probe.progress.timeout_ms) return 0
  const elapsedRatio = probe.progress.elapsed_ms / probe.progress.timeout_ms
  if (
    probe.status === 'queued'
    || probe.status === 'running'
    || probe.status === 'cleaning'
    || probe.status === 'cancelling'
  ) {
    return Math.min(0.99, elapsedRatio)
  }
  return Math.min(1, elapsedRatio)
}

function MagnetProbeProgress({ probe }: { probe: MagnetProbePayload }) {
  const availableCount = probe.items.filter((item) => item.seed_status === 'available').length
  const timeoutSeconds = Math.max(0, Math.ceil(probe.progress.timeout_ms / 1000))
  const elapsedSeconds = Math.max(0, Math.floor(probe.progress.elapsed_ms / 1000))
  return (
    <div className="magnet-probe-progress">
      <div>
        {probe.status === 'queued' ? <span>任务已排队</span> : null}
        {probe.status === 'running' ? <span>任务用时 {elapsedSeconds} 秒</span> : null}
        {probe.status === 'cleaning' ? <span>观察已结束，正在清理临时任务</span> : null}
        {probe.status === 'cancelling' ? <span>正在停止探测</span> : null}
        {probe.status === 'complete' ? <span>已完成 {probe.total} / {probe.total}</span> : null}
        {probe.status === 'failed' ? <span>探测失败</span> : null}
        {probe.status === 'cancelled' ? <span>探测已取消</span> : null}
        {probe.status !== 'queued' && probe.status !== 'running' ? <span>任务用时 {elapsedSeconds} 秒</span> : null}
        {probe.status === 'queued' || probe.status === 'running' ? <span>最长观察时间 {timeoutSeconds} 秒</span> : null}
        {availableCount ? <span>已发现 {availableCount} 条有做种</span> : null}
        {probe.cleanup?.status === 'incomplete' ? <span className="gallery-failure-count">临时任务清理不完整</span> : null}
      </div>
      <ProgressBar value={probeProgressValue(probe)} label="磁链探测任务进度" />
      <span className="probe-announcement" role="status" aria-live="polite" aria-atomic="true">
        {probeAnnouncement(probe, availableCount)}
      </span>
    </div>
  )
}

function selectionProgressValue(selection: MagnetSelectionPayload): number {
  if (selection.status === 'complete') return 1
  if (!selection.progress.timeout_ms) return 0
  const elapsedRatio = selection.progress.elapsed_ms / selection.progress.timeout_ms
  if (selection.status === 'queued' || selection.status === 'running' || selection.status === 'cleaning' || selection.status === 'cancelling') {
    return Math.min(0.99, elapsedRatio)
  }
  return Math.min(1, elapsedRatio)
}

function selectionAnnouncement(selection: MagnetSelectionPayload): string {
  if (selection.status === 'queued') return '智能选种已排队'
  if (selection.status === 'running') return '正在观察候选磁链并比较画质与速度'
  if (selection.status === 'cleaning') return '已选出最优种子，正在删除其他任务和文件'
  if (selection.status === 'cancelling') return '正在取消智能选种并清理候选任务'
  if (selection.status === 'complete') {
    return selection.selection.status === 'selected'
      ? `已选中 ${selection.selection.selected_quality || '可用'} 种子`
      : '未找到可用种子'
  }
  if (selection.status === 'failed') {
    return selection.selection.status === 'inconclusive'
      ? '部分候选尚未被 qBittorrent 调度，未将其判定为不可用'
      : '智能选种失败'
  }
  return '智能选种已取消'
}

function MagnetSelectionProgress({ selection }: { selection: MagnetSelectionPayload }) {
  const selected = selection.selection.status === 'selected'
  const selectedQuality = selection.selection.selected_quality
  const timeoutSeconds = Math.max(0, Math.ceil(selection.progress.timeout_ms / 1000))
  const elapsedSeconds = Math.max(0, Math.floor(selection.progress.elapsed_ms / 1000))
  const usableCount = selection.items.filter((item) => (
    (item.seeders ?? 0) > 0 || (item.connected_seeders ?? 0) > 0 || (item.download_speed ?? 0) > 0
  )).length
  return (
    <div className="magnet-probe-progress magnet-selection-progress">
      <div>
        {selection.status === 'queued' ? <span>任务已排队</span> : null}
        {selection.status === 'running' ? <span>任务用时 {elapsedSeconds} 秒</span> : null}
        {selection.status === 'cleaning' ? <span>观察已结束，正在清理其他任务</span> : null}
        {selection.status === 'cancelling' ? <span>正在停止候选任务</span> : null}
        {selection.status === 'complete' ? <span>{selected ? '已完成选种' : '选种未找到可用资源'}</span> : null}
        {selection.status === 'failed' ? (
          <span>
            {selection.selection.status === 'inconclusive'
              ? '候选未全部完成探测'
              : '选种失败'}
          </span>
        ) : null}
        {selection.status === 'cancelled' ? <span>选种已取消</span> : null}
        {selection.status !== 'queued' && selection.status !== 'running' ? <span>任务用时 {elapsedSeconds} 秒</span> : null}
        {selection.status === 'queued' || selection.status === 'running' ? <span>最长观察时间 {timeoutSeconds} 秒</span> : null}
        {usableCount ? <span>当前有 {usableCount} 条可用候选</span> : null}
        {selectedQuality ? <span>胜者画质 {selectedQuality}</span> : null}
        {selection.cleanup?.status === 'incomplete' ? <span className="gallery-failure-count">其他任务清理不完整</span> : null}
      </div>
      <ProgressBar value={selectionProgressValue(selection)} label="智能选种任务进度" />
      <span className="probe-announcement" role="status" aria-live="polite" aria-atomic="true">
        {selectionAnnouncement(selection)}
      </span>
    </div>
  )
}

function webDownloadStatusDescription(job: WebDownloadJob): string {
  const continueHint = webDownloadContinueHint(job)
  if (job.status === 'queued') return '任务正在等待可用下载位。'
  if (job.status === 'retry_wait') {
    return `${job.error || '媒体连接暂时中断'}。${webDownloadRetryStatus(job) || '等待自动恢复'}。`
  }
  if (job.status === 'locating') return '正在定位可用的视频源。'
  if (job.status === 'validating') return '正在确认片源与番号匹配。'
  if (job.status === 'downloading') return '视频正在下载到独立的 Web 暂存目录。'
  if (job.status === 'verifying') return '下载完成，正在验证文件。'
  if (job.status === 'archiving') return '正在移动到媒体库。'
  if (job.status === 'completed' && job.archive_status === 'missing') {
    return '归档文件已删除，请前往下载页清理对应记录。'
  }
  if (job.status === 'completed' && job.archive_status === 'unknown') {
    return '归档状态暂时无法核验。'
  }
  if (job.status === 'completed') return '视频已完成归档。'
  if (job.status === 'failed') {
    const failure = job.error || '下载未完成'
    return continueHint ? `${failure}；${continueHint}` : `${failure}。`
  }
  if (job.status === 'cancelling') return '正在安全停止任务。'
  if (job.status === 'cancelled') return continueHint
    ? `任务已取消。${continueHint}`
    : '任务已取消。'
  return job.error || `任务状态：${job.status || '未知'}`
}

function webDownloadStatusPresentation(job: WebDownloadJob) {
  if (job.status === 'completed' && job.archive_status === 'missing') {
    return { label: '归档文件已删除', tone: 'danger' as const }
  }
  if (job.status === 'completed' && job.archive_status === 'unknown') {
    return { label: '归档状态待确认', tone: 'neutral' as const }
  }
  return {
    label: webDownloadStatusLabel(job.status),
    tone: webDownloadStatusTone(job.status),
  }
}

function webDownloadCacheCode(code: string): string {
  return code.trim().toUpperCase().replace(/[^A-Z0-9]/g, '')
}

function webDownloadIntentIsActive(intent: WebDownloadBatch | null | undefined): boolean {
  return Boolean(intent && ACTIVE_WEB_DOWNLOAD_INTENT_STATUSES.has(intent.status))
}

function webDownloadIntentNeedsPolling(
  intent: WebDownloadBatch | null | undefined,
  nowMs = Date.now(),
): boolean {
  if (!intent) return false
  if (webDownloadIntentIsActive(intent)) return true
  if (intent.status !== 'committed' || !intent.items.some((item) => Boolean(item.job_id))) return false
  const numericUpdatedAt = Number(intent.updated_at)
  const updatedAtMs = Number.isFinite(numericUpdatedAt) && numericUpdatedAt > 0
    ? (numericUpdatedAt < 10_000_000_000 ? numericUpdatedAt * 1_000 : numericUpdatedAt)
    : Date.parse(String(intent.updated_at))
  if (!Number.isFinite(updatedAtMs) || updatedAtMs <= 0) return false
  const ageMs = nowMs - updatedAtMs
  return ageMs >= 0 && ageMs <= WEB_DOWNLOAD_INTENT_BRIDGE_MS
}

function webDownloadIntentDescription(intent: WebDownloadBatch): string {
  if (webDownloadIntentIsActive(intent)) return '已加入后台，正在解析可用版本与画质。'
  if (intent.status === 'failed') return intent.error || '未找到可用的 Web 视频资源。'
  if (intent.status === 'committed') {
    if (intent.created_count > 0) return 'Web 下载任务已创建。'
    if (intent.reused_count > 0) return '已复用同番号的 Web 下载任务。'
    if (intent.skipped_count > 0) return '已有作品符合当前策略，未重复创建任务。'
    return '后台请求已处理。'
  }
  if (intent.status === 'cancelled') return '后台请求已取消。'
  return intent.error || '后台请求暂未完成。'
}

function webDownloadIntentPresentation(intent: WebDownloadBatch) {
  if (intent.status === 'failed') return { label: '后台解析失败', tone: 'danger' as const }
  if (intent.status === 'committed') return { label: '已提交', tone: 'success' as const }
  if (intent.status === 'cancelled') return { label: '已取消', tone: 'warning' as const }
  return { label: intent.status === 'queued' ? '等待后台解析' : '后台解析中', tone: 'info' as const }
}

function webDownloadSubmissionMessage(code: string, _job: WebDownloadJob): string {
  // The direct endpoint is idempotent: a response can represent either a newly
  // created job or an existing matching job. Keep the confirmation neutral so
  // the UI never claims that a duplicate was created (or skipped).
  return `${code} Web 下载已加入后台`
}

export function DetailPage() {
  const { workId = '' } = useParams()
  const location = useLocation()
  const navigationState = location.state as {
    work?: WorkResult
    returnTo?: string
    fromSearch?: boolean
  } | null
  const [routeSearchParams] = useSearchParams()
  const detailSearchParams = new URLSearchParams(routeSearchParams)
  detailSearchParams.delete('replacement_id')
  const lookupCode = detailSearchParams.get('code')?.trim() || ''
  const routeWork = navigationState?.work
  const fallbackSearchParams = new URLSearchParams(detailSearchParams)
  fallbackSearchParams.delete('code')
  const fallbackReturnTo = fallbackSearchParams.get('q')
    ? `/results?${fallbackSearchParams.toString()}`
    : '/search'
  const returnTo = navigationState?.returnTo === '/search'
    || navigationState?.returnTo?.startsWith('/search?')
    || navigationState?.returnTo === '/results'
    || navigationState?.returnTo?.startsWith('/results?')
    || navigationState?.returnTo === '/downloads?view=archive'
    ? navigationState.returnTo
    : fallbackReturnTo
  const returnLabel = returnTo === '/downloads?view=archive'
    ? '返回失败归档'
    : '返回搜索'
  const sourceScope = canonicalSourceScope(detailSearchParams.get('source') || 'all')
  const recoveryScope = workRecoveryScope(workId, detailSearchParams)
  const navigate = useNavigate()
  const toast = useToast()
  const queryClient = useQueryClient()
  const workQuery = useQuery({
    queryKey: ['work', workId, lookupCode, sourceScope, recoveryScope],
    queryFn: ({ signal }) => api.work(workId, detailSearchParams, signal),
    enabled: Boolean(workId),
    placeholderData: routeWork,
    staleTime: (query) => hasSourceError(query.state.data) ? 0 : 15 * 60_000,
    refetchOnMount: (query) => hasSourceError(query.state.data) ? 'always' : true,
    gcTime: 60 * 60_000,
  })
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const [activeSourceSelection, setActiveSourceSelection] = useState({ workId: '', sourceId: '' })
  const [lightboxIndex, setLightboxIndex] = useState<number | null>(null)
  const [imageLoadStates, setImageLoadStates] = useState<Record<string, Record<string, ImageLoadState>>>({})
  const [downloadStates, setDownloadStates] = useState<Record<string, DownloadState>>({})
  const [probeId, setProbeId] = useState('')
  const [probeOwnerWorkId, setProbeOwnerWorkId] = useState(workId)
  const [probeSeed, setProbeSeed] = useState<MagnetProbePayload | null>(null)
  const [probeStarting, setProbeStarting] = useState(false)
  const [probeStartError, setProbeStartError] = useState('')
  const [selectionId, setSelectionId] = useState('')
  const [selectionOwnerWorkId, setSelectionOwnerWorkId] = useState(workId)
  const [selectionSeed, setSelectionSeed] = useState<MagnetSelectionPayload | null>(null)
  const [selectionStarting, setSelectionStarting] = useState(false)
  const [selectionStartError, setSelectionStartError] = useState('')
  const dialogRef = useRef<HTMLDialogElement | null>(null)
  const probeGenerationRef = useRef(0)
  const probeWorkIdRef = useRef(workId)
  const selectionGenerationRef = useRef(0)
  const selectionWorkIdRef = useRef(workId)
  const webDownloadCodeRef = useRef('')
  const [webDownloadConfirm, setWebDownloadConfirm] = useState('')
  const [checkingWebHistory, setCheckingWebHistory] = useState(false)
  const webDownloadAttemptRef = useRef<{ code: string; idempotencyKey: string } | null>(null)
  const probeQuery = useQuery({
    queryKey: ['magnet-probe', workId, probeId],
    queryFn: () => api.magnetProbe(probeId),
    enabled: Boolean(probeId) && probeOwnerWorkId === workId,
    refetchInterval: (query) => {
      const payload = query.state.data as MagnetProbePayload | undefined
      return payload && TERMINAL_PROBE_STATUSES.has(payload.status) ? false : 750
    },
    refetchIntervalInBackground: false,
    retry: false,
  })
  const selectionQuery = useQuery({
    queryKey: ['magnet-selection', workId, selectionId],
    queryFn: () => api.magnetSelection(selectionId),
    enabled: Boolean(selectionId) && selectionOwnerWorkId === workId,
    refetchInterval: (query) => {
      const payload = query.state.data as MagnetSelectionPayload | undefined
      return payload && TERMINAL_SELECTION_STATUSES.has(payload.status) ? false : 1_500
    },
    refetchIntervalInBackground: false,
    retry: false,
  })

  const work = workQuery.data ?? routeWork
  const translation = useTranslationPreferences(settings.data?.settings.workflow_defaults?.translation)
  const titleTranslations = useTranslations(work?.title ? [work.title] : [], translation.preferences.enabled)
  const translatedTitle = translation.preferences.enabled && work ? titleTranslations.translate(work.title) : null
  const aiTitles = useAiTranslations(work?.title ? [work.title] : [])
  const canonicalCode = work?.canonical_code?.trim() || ''
  webDownloadCodeRef.current = canonicalCode
  const webDownloadQueryCode = webDownloadCacheCode(canonicalCode)
  const hasWebDownloadCode = Boolean(webDownloadQueryCode)
  const webDownloadsQuery = useQuery({
    queryKey: ['web-downloads', 'code', webDownloadQueryCode],
    queryFn: () => api.webDownloads({ code: webDownloadQueryCode }),
    enabled: hasWebDownloadCode,
    refetchInterval: (query) => {
      const payload = query.state.data as WebDownloadListPayload | undefined
      if (webDownloadIntentIsActive(payload?.intent)) return 1_000
      const job = currentWebDownloadJob(payload?.tasks ?? [])
      if (job) return webDownloadPollInterval(job, 1_500)
      return webDownloadIntentNeedsPolling(payload?.intent) ? 1_000 : false
    },
    refetchIntervalInBackground: false,
    retry: false,
  })
  const updateWebDownloadCache = useCallback((code: string, job: WebDownloadJob) => {
    const cacheCode = webDownloadCacheCode(code)
    if (!cacheCode) return
    queryClient.setQueryData<WebDownloadListPayload>(['web-downloads', 'code', cacheCode], (current) => ({
      ...current,
      ok: true,
      configured: current?.configured ?? true,
      tasks: [job, ...(current?.tasks ?? []).filter((item) => item.job_id !== job.job_id)],
      // A direct submission supersedes any legacy automatic-intent snapshot
      // returned for this code; do not let a stale intent hide the job we just
      // received.
      intent: null,
    }))
    void queryClient.invalidateQueries({ queryKey: WEB_DOWNLOAD_HISTORY_QUERY_ROOT })
  }, [queryClient])
  const startWebDownload = useMutation({
    mutationFn: ({ code, idempotencyKey }: { code: string; idempotencyKey: string }) => (
      api.addWebDownload(code, idempotencyKey)
    ),
    onSuccess: (job, variables) => {
      updateWebDownloadCache(variables.code, job)
      if (webDownloadCacheCode(variables.code) !== webDownloadCacheCode(webDownloadCodeRef.current)) return
      webDownloadAttemptRef.current = null
      toast.push(webDownloadSubmissionMessage(variables.code, job), 'success')
    },
    onError: (error, variables) => {
      if (webDownloadCacheCode(variables.code) === webDownloadCacheCode(webDownloadCodeRef.current)) {
        toast.push((error as Error).message, 'error')
      }
    },
  })
  const continueWebDownload = useMutation({
    mutationFn: (jobId: string) => api.webDownloadAction(jobId, 'retry'),
    onSuccess: (payload) => {
      if ('code' in payload) updateWebDownloadCache(payload.code, payload)
      else void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      toast.push('Web 下载已继续排队', 'success')
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const cancelWebDownload = useMutation({
    mutationFn: (jobId: string) => api.webDownloadAction(jobId, 'cancel'),
    onSuccess: (payload) => {
      if ('code' in payload) updateWebDownloadCache(payload.code, payload)
      else void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      toast.push('正在取消 Web 下载', 'success')
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const prepareMagnetReplacement = useMutation({
    mutationFn: (candidate: DownloadReselection) => (
      api.createDownloadReplacement(candidate.source_kind, candidate.source_id, 'manual_magnet')
    ),
    onSuccess: ({ replacement }) => {
      void queryClient.invalidateQueries({ queryKey: ['downloads'] })
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      if (replacement.status !== 'open' || replacement.discovery_status === 'available') {
        toast.push(
          replacement.status === 'completed'
            ? `${replacement.code} 的旧 Web 失败项已处理，无需再次选择来源`
            : replacement.status === 'expired'
              ? '换源入口已失效，请刷新详情后重试'
              : '换源任务正在处理，请稍后刷新下载列表',
          'info',
        )
        return
      }

      toast.push('已开始探测可用磁链，可在下载列表直接选择', 'info')
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const sites = useMemo(
    () => settings.data?.settings.sites.filter((site) => site.enabled) ?? [],
    [settings.data?.settings.sites],
  )
  const sourceScreenshots = useMemo(
    () => new Map((work?.sources ?? []).map((source) => [source.source_id, uniqueSampleImages(source.images)])),
    [work?.sources],
  )
  const manuallySelectedSourceId = activeSourceSelection.workId === workId
    ? activeSourceSelection.sourceId
    : ''
  const defaultDetailSourceId = settings.data?.settings.detail_default_site_id?.trim() || ''
  const activeSource = work?.sources.find((source) => source.source_id === manuallySelectedSourceId)
    || work?.sources.find((source) => source.source_id === defaultDetailSourceId)
    || work?.sources[0]
  const resolvedActiveSource = workQuery.data?.sources.find(
    (source) => source.source_id === activeSource?.source_id,
  )
  const activeSourceError = workQuery.dataUpdatedAt > 0
    && !workQuery.isPlaceholderData
    && !workQuery.isFetching
    ? resolvedActiveSource?.error
      || resolvedActiveSource?.details_error
      || resolvedActiveSource?.image_error
      || resolvedActiveSource?.magnet_error
    : null
  const images = activeSource ? sourceScreenshots.get(activeSource.source_id) ?? [] : []
  const lightboxImage = lightboxIndex === null ? null : images[lightboxIndex]
  const activeImageStates = activeSource ? imageLoadStates[activeSource.source_id] ?? {} : {}
  const probe = probeOwnerWorkId === workId ? probeQuery.data ?? probeSeed : null
  const probeActive = probeStarting || Boolean(probe && !TERMINAL_PROBE_STATUSES.has(probe.status))
  const probeItems = Object.fromEntries((probe?.items ?? []).map((item) => [item.info_hash, item]))
  const selection = selectionOwnerWorkId === workId ? selectionQuery.data ?? selectionSeed : null
  const selectionActive = selectionStarting || Boolean(selection && !TERMINAL_SELECTION_STATUSES.has(selection.status))
  const selectionItems = Object.fromEntries((selection?.items ?? []).map((item) => [item.info_hash, item]))
  const webDownloadTasks = webDownloadsQuery.data?.tasks ?? []
  const webDownloadIntent = webDownloadsQuery.data?.intent ?? null
  const webDownloadIntentActive = webDownloadIntentIsActive(webDownloadIntent)
  const webDownloadIntentView = webDownloadIntent ? webDownloadIntentPresentation(webDownloadIntent) : null
  const webDownloadJob = currentWebDownloadJob(webDownloadTasks)
  const webDownloadReselection = webDownloadJob?.reselection
    || webDownloadIntent?.reselection
    || (webDownloadJob && webDownloadCanReselect(webDownloadJob) && webDownloadJob.job_id
      ? { source_kind: 'web_job' as const, source_id: webDownloadJob.job_id, code: webDownloadJob.code || canonicalCode }
      : webDownloadIntent
        && !ACTIVE_WEB_DOWNLOAD_INTENT_STATUSES.has(webDownloadIntent.status)
        && (
          ['failed', 'incomplete', 'too_many', 'cancelled'].includes(webDownloadIntent.status)
          || Boolean(webDownloadIntent.error?.trim())
        )
        ? { source_kind: 'web_intent' as const, source_id: webDownloadIntent.batch_id, code: webDownloadIntent.code_or_prefix || canonicalCode }
        : null)
  const activeWebDownloadJob = webDownloadJob && webDownloadIsActive(webDownloadJob) ? webDownloadJob : null
  const webDownloadPresentation = webDownloadJob ? webDownloadStatusPresentation(webDownloadJob) : null
  const webDownloadSummaryPresentation = webDownloadIntentActive
    ? webDownloadIntentView
    : webDownloadPresentation ?? webDownloadIntentView
  const webDownloadReady = Boolean(
    webDownloadsQuery.data?.ok
    && webDownloadsQuery.data.configured
    && webDownloadsQuery.data.enabled !== false
    && webDownloadsQuery.data.available !== false,
  )
  const webDownloadWarning = webDownloadsQuery.isError
    ? (webDownloadsQuery.error as Error).message
    : webDownloadsQuery.data?.configured && !webDownloadsQuery.data.ok
      ? webDownloadsQuery.data.error || '无法读取 Web 下载状态'
      : webDownloadsQuery.data && !webDownloadReady
        ? webDownloadsQuery.data.configured ? 'Web 视频下载当前不可用。' : 'Web 视频下载尚未配置。'
        : webDownloadIntent?.status === 'failed' && !activeWebDownloadJob
          ? webDownloadIntent.error || '未找到可用的 Web 视频资源。'
          : ''
  const markImageState = useCallback((sourceId: string, key: string, state: ImageLoadState) => {
    setImageLoadStates((current) => {
      const sourceStates = current[sourceId] ?? {}
      if (sourceStates[key] === state) return current
      return {
        ...current,
        [sourceId]: { ...sourceStates, [key]: state },
      }
    })
  }, [])

  useEffect(() => {
    setLightboxIndex(null)
  }, [activeSource?.source_id])

  useEffect(() => setImageLoadStates({}), [workId, workQuery.dataUpdatedAt])

  useLayoutEffect(() => {
    webDownloadAttemptRef.current = null
    startWebDownload.reset()
    setWebDownloadConfirm('')
  }, [canonicalCode])

  useLayoutEffect(() => {
    probeWorkIdRef.current = workId
    probeGenerationRef.current += 1
    setProbeOwnerWorkId(workId)
    setProbeId('')
    setProbeSeed(null)
    setProbeStarting(false)
    setProbeStartError('')
    return () => {
      probeGenerationRef.current += 1
    }
  }, [workId])

  useLayoutEffect(() => {
    selectionWorkIdRef.current = workId
    selectionGenerationRef.current += 1
    setSelectionOwnerWorkId(workId)
    setSelectionId('')
    setSelectionSeed(null)
    setSelectionStarting(false)
    setSelectionStartError('')
    return () => {
      selectionGenerationRef.current += 1
    }
  }, [workId])

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (lightboxIndex !== null && !dialog.open) {
      if (typeof dialog.showModal === 'function') dialog.showModal()
      else dialog.setAttribute('open', '')
    }
    if (lightboxIndex === null && dialog.open) {
      if (typeof dialog.close === 'function') dialog.close()
      else dialog.removeAttribute('open')
    }
  }, [lightboxIndex])

  function submitWebDownload() {
    if (!canonicalCode || !webDownloadReady || startWebDownload.isPending) return
    const code = canonicalCode
    const currentAttempt = webDownloadAttemptRef.current
    const attempt = currentAttempt && webDownloadCacheCode(currentAttempt.code) === webDownloadCacheCode(code)
      ? currentAttempt
      : { code, idempotencyKey: createWebDownloadIntentKey() }
    webDownloadAttemptRef.current = attempt
    startWebDownload.mutate(attempt)
  }



  async function requestWebDownload() {
    if (!canonicalCode || !webDownloadReady || startWebDownload.isPending || checkingWebHistory) return
    const code = canonicalCode
    setWebDownloadConfirm('')
    setCheckingWebHistory(true)
    try {
      const lookup = await api.downloadHistoryLookup([code])
      const summary = downloadHistorySummary(lookup.items[0])
      if (summary && webDownloadCacheCode(code) === webDownloadCacheCode(webDownloadCodeRef.current)) {
        setWebDownloadConfirm(summary)
        return
      }
    } catch {
      // The history check is advisory; the Web queue still reuses matching jobs.
    } finally {
      setCheckingWebHistory(false)
    }
    submitWebDownload()
  }

  function relatedSearchHref(item: RelatedRef, sourceId = ''): string {
    const metadataSites = sites.filter((site) => (
      site.capabilities.includes('metadata_search')
      && METADATA_PARSER_PROFILES.has(site.parser_profile)
    ))
    const metadataSourceIds = new Set(metadataSites.map((site) => site.id))
    const metadataSitesById = new Map(metadataSites.map((site) => [site.id, site]))
    const selectedSources = detailSearchParams.get('source')?.split(',').filter((id) => metadataSourceIds.has(id)) ?? []
    const fallbackSources = (work?.sources.map((source) => source.source_id) ?? [sourceId].filter(Boolean))
      .filter((id) => metadataSourceIds.has(id))
    const sources = Array.from(new Set(selectedSources.length ? selectedSources : fallbackSources)).sort()
    const params = new URLSearchParams({
      q: item.label,
      source: sources.join(','),
      result_limit: detailSearchParams.get('result_limit') || '20',
      page: '1',
      magnets: detailSearchParams.get('magnets') === '0' ? '0' : '1',
      sort: 'relevance',
      match: 'fuzzy',
      kind: item.kind,
      site_mode: metadataSites.length && sources.length === metadataSites.length ? 'all' : 'custom',
    })
    const sourceRefs = new Map<string, string>()
    const sourceReference = sourceId && item.url
      ? normalizeRelatedSearchReference(item.url, metadataSitesById.get(sourceId))
      : null
    if (sourceId && sourceReference) sourceRefs.set(sourceId, sourceReference)
    const detailKey = RELATED_KEY_BY_KIND[item.kind]
    if (detailKey) {
      work?.sources.forEach((source) => {
        const related = source.details?.[detailKey]
        if (!Array.isArray(related) || !sources.includes(source.source_id)) return
        const matching = (related as RelatedRef[]).find((candidate) => relatedLabelsMatch(candidate, item))
        const reference = matching?.url
          ? normalizeRelatedSearchReference(matching.url, metadataSitesById.get(source.source_id))
          : null
        if (reference) sourceRefs.set(source.source_id, reference)
      })
    }
    sourceRefs.forEach((url, relatedSourceId) => {
      if (sources.includes(relatedSourceId)) params.set(`ref.${relatedSourceId}`, url)
    })
    return `/results?${params.toString()}`
  }

  function relatedRefForLabel(kind: 'actor' | 'tag', label: string): RelatedRef {
    const detailKey = RELATED_KEY_BY_KIND[kind]
    const preferred = detailKey ? activeSource?.details?.[detailKey] : undefined
    const item = Array.isArray(preferred)
      ? (preferred as RelatedRef[]).find((candidate) => candidate.kind === kind && candidate.label === label)
      : undefined
    if (item) return item
    for (const source of work?.sources ?? []) {
      const related = detailKey ? source.details?.[detailKey] : undefined
      const candidate = Array.isArray(related)
        ? (related as RelatedRef[]).find((value) => value.kind === kind && value.label === label)
        : undefined
      if (candidate) return candidate
    }
    return { kind, label, url: null }
  }

  async function addDownload(magnet: WorkMagnet) {
    if (!work) return
    const action = canonicalMagnetAction(magnet)
    const source = action.primary
    if (!source || !action.uri) return
    setDownloadStates((current) => ({ ...current, [magnet.info_hash]: 'adding' }))
    try {
      const added = await api.addDownload({
        magnet: action.uri,
        name: magnet.display_name || source.display_name || work.code || work.title,
        category: '',
        save_path: '',
        tags: '',
        auto_organize: true,
        result: work,
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
      const metadataNotice = added.metadata_warning ? '；自动元数据登记失败，完成后请手动补全' : ''
      toast.push(
        `已添加 ${added.display_name || work.code}${metadataNotice}`,
        added.metadata_warning ? 'info' : 'success',
      )
    } catch (error) {
      setDownloadStates((current) => ({ ...current, [magnet.info_hash]: 'error' }))
      toast.push((error as Error).message, 'error')
    }
  }

  async function startMagnetProbe() {
    if (!work?.magnets.length || probeActive) return
    const magnets = Array.from(new Set(
      work.magnets.map((magnet) => canonicalMagnetAction(magnet).uri).filter(Boolean),
    ))
    if (!magnets.length) return
    const requestedWorkId = workId
    const requestGeneration = probeGenerationRef.current + 1
    probeGenerationRef.current = requestGeneration
    const requestIsCurrent = () => probeWorkIdRef.current === requestedWorkId
      && probeGenerationRef.current === requestGeneration
    setProbeStarting(true)
    setProbeOwnerWorkId(requestedWorkId)
    setProbeStartError('')
    try {
      const started = await api.startMagnetProbe(magnets)
      if (!requestIsCurrent()) return
      setProbeSeed(started)
      setProbeId(started.probe_id)
    } catch (error) {
      if (!requestIsCurrent()) return
      setProbeStartError((error as Error).message)
    } finally {
      if (requestIsCurrent()) setProbeStarting(false)
    }
  }

  async function startMagnetSelection() {
    if (!work?.magnets.length || probeActive || selectionActive) return
    const magnets = Array.from(new Set(
      work.magnets.map((magnet) => canonicalMagnetAction(magnet).uri).filter(Boolean),
    ))
    if (!magnets.length) return
    const requestedWorkId = workId
    const requestGeneration = selectionGenerationRef.current + 1
    selectionGenerationRef.current = requestGeneration
    const requestIsCurrent = () => selectionWorkIdRef.current === requestedWorkId
      && selectionGenerationRef.current === requestGeneration
    setSelectionStarting(true)
    setSelectionOwnerWorkId(requestedWorkId)
    setSelectionStartError('')
    try {
      const started = await api.startMagnetSelection(magnets)
      if (!requestIsCurrent()) return
      setSelectionSeed(started)
      setSelectionId(started.selection_id)
    } catch (error) {
      if (!requestIsCurrent()) return
      setSelectionStartError((error as Error).message)
    } finally {
      if (requestIsCurrent()) setSelectionStarting(false)
    }
  }

  function activateSource(source: WorkSource) {
    setActiveSourceSelection({ workId, sourceId: source.source_id })
  }

  function handleTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, index: number) {
    if (!work?.sources.length) return
    let nextIndex: number | null = null
    if (event.key === 'ArrowRight' || event.key === 'ArrowDown') nextIndex = (index + 1) % work.sources.length
    if (event.key === 'ArrowLeft' || event.key === 'ArrowUp') nextIndex = (index - 1 + work.sources.length) % work.sources.length
    if (event.key === 'Home') nextIndex = 0
    if (event.key === 'End') nextIndex = work.sources.length - 1
    if (nextIndex === null) return
    event.preventDefault()
    const nextSource = work.sources[nextIndex]
    activateSource(nextSource)
    document.getElementById(`work-source-tab-${nextSource.source_id}`)?.focus()
  }

  function moveLightbox(offset: number) {
    if (lightboxIndex === null || !images.length) return
    setLightboxIndex((lightboxIndex + offset + images.length) % images.length)
  }

  function handleDialogKeyDown(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.key === 'ArrowLeft') moveLightbox(-1)
    if (event.key === 'ArrowRight') moveLightbox(1)
  }

  function returnToSearch() {
    navigate(returnTo, { replace: true })
  }

  if (workQuery.isLoading && !work) return <div className="page detail-page"><SkeletonRows count={5} /></div>

  if (workQuery.isError && !work) {
    return (
      <div className="page detail-page">
        <PageHeader title={lookupCode || workId || '作品详情'} description="聚合作品资料" />
        <EmptyState
          role="alert"
          title="无法加载作品详情"
          description={(workQuery.error as Error).message}
          action={<Button type="button" onClick={() => void workQuery.refetch()}>重新加载</Button>}
        />
      </div>
    )
  }

  if (!work) return <div className="page detail-page"><EmptyState title="未找到作品" /></div>

  const displayCode = work.code || work.canonical_code || lookupCode || '未知番号'
  const cover = workCover(work, true)
  const coverUrl = cover ? coverImageUrl(cover.sourceId, cover.url, sites) : ''
  const backdrop = workBackdrop(work, true)
  const backdropUrl = backdrop ? coverImageUrl(backdrop.sourceId, backdrop.url, sites) : ''
  return (
    <div className="page detail-page">
      <PageHeader
        title={displayCode}
        description="聚合作品详情"
        actions={
          <>
            {workQuery.isFetching ? (
              <StatusBadge tone="info">
                <span role="status">正在补充截图与磁链</span>
              </StatusBadge>
            ) : null}
            <Button
              type="button"
              size="small"
              variant="secondary"
              onClick={() => void workQuery.refetch()}
              disabled={workQuery.isFetching}
            >
              <RotateCcw className={workQuery.isFetching ? 'spin' : ''} aria-hidden="true" />
              刷新详情
            </Button>
            <Button type="button" size="small" variant="secondary" onClick={returnToSearch}>
              <ArrowLeft aria-hidden="true" />
              {returnLabel}
            </Button>
          </>
        }
      />

      {workQuery.isError && work ? (
        <InlineNotice tone="warning" role="alert">
          <strong>完整详情加载失败</strong>
          <span>{(workQuery.error as Error).message}</span>
          <Button type="button" size="small" onClick={() => void workQuery.refetch()} disabled={workQuery.isFetching}>
            重新加载
          </Button>
        </InlineNotice>
      ) : null}

      <div className={`work-overview${backdrop ? ' has-backdrop' : ''}`}>
        <div className="work-cover">
          <StableImage
            src={coverUrl}
            alt={`${displayCode} 封面`}
            retryToken={workQuery.dataUpdatedAt}
            referrerPolicy="no-referrer"
            loading="eager"
            fetchPriority="high"
          />
        </div>
        {backdrop ? (
          <div className="work-backdrop">
            <StableImage
              src={backdropUrl}
              alt={`${displayCode} 横版主图`}
              retryToken={workQuery.dataUpdatedAt}
              referrerPolicy="no-referrer"
              loading="eager"
            />
          </div>
        ) : null}
        <div className="work-overview-content">
          <div className="work-overview-heading">
            <div>
              <span className="work-code">{displayCode}</span>
              <h2>{translatedTitle || work.title}</h2>
              {translatedTitle && translation.preferences.showOriginal ? <p className="work-original-title">{work.title}</p> : null}
              <AiTranslationLine text={aiTitles.translate(work.title)} className="work-ai-title" />
              <div className="work-translate-actions">
                <button
                  type="button"
                  className="work-translate-toggle"
                  aria-pressed={translation.preferences.enabled}
                  onClick={() => translation.update({ enabled: !translation.preferences.enabled })}
                >
                  {translation.preferences.enabled ? (titleTranslations.loading ? '正在翻译…' : '显示原标题') : '翻译标题'}
                </button>
                <AiTranslateButton state={aiTitles} appearance="link" />
              </div>
            </div>
            <div className="work-source-summary">
              {work.sources.map((source) => <StatusBadge key={source.source_id}>{sourceName(source.source_id, sites)}</StatusBadge>)}
            </div>
          </div>
          <dl className="work-facts">
            <div><dt>发行日期</dt><dd>{work.release_date || '未知'}</dd></div>
            <div><dt>站点来源</dt><dd>{work.sources.length}</dd></div>
            <div><dt>磁链数量</dt><dd>{work.magnets.length}</dd></div>
          </dl>
          {work.release_date_conflict ? (
            <InlineNotice tone="warning">
              <strong>发行日期存在来源差异</strong>
              <span>可在下方站点标签中核对各来源记录。</span>
            </InlineNotice>
          ) : null}
          {work.actors.length ? (
            <div className="work-terms" aria-label="演员">
              {work.actors.map((actor) => {
                const item = relatedRefForLabel('actor', actor)
                return <Link to={relatedSearchHref(item)} key={actor}>{actor}</Link>
              })}
            </div>
          ) : null}
          {work.tags.length ? (
            <div className="work-terms muted" aria-label="标签">
              {work.tags.map((tag) => {
                const item = relatedRefForLabel('tag', tag)
                return <Link to={relatedSearchHref(item)} key={tag}>{tag}</Link>
              })}
            </div>
          ) : null}
          {hasWebDownloadCode ? (
            <div className="work-web-download" aria-label="Web 视频下载">
              <div className="work-web-download-head">
                <Download aria-hidden="true" />
                <strong>Web 下载</strong>
                {webDownloadSummaryPresentation ? (
                  <StatusBadge tone={webDownloadSummaryPresentation.tone}>
                    {webDownloadSummaryPresentation.label}
                  </StatusBadge>
                ) : null}
                <span className="work-web-download-note">
                  {webDownloadIntentActive && webDownloadIntent
                    ? webDownloadIntentDescription(webDownloadIntent)
                    : webDownloadJob
                      ? webDownloadStatusDescription(webDownloadJob)
                      : webDownloadIntent
                        ? webDownloadIntentDescription(webDownloadIntent)
                        : webDownloadsQuery.isLoading
                          ? '正在检查是否已有同番号任务。'
                          : '按站点优先级自动获取 Web 视频。'}
                </span>
              </div>
              {webDownloadJob ? (
                <div className="work-web-download-progress">
                  <ProgressBar
                    value={webDownloadProgress(webDownloadJob)}
                    label={`${webDownloadJob.code} ${webDownloadVariantLabel(webDownloadJob.variant)} Web 下载进度`}
                  />
                  <span>
                    {webDownloadVariantLabel(webDownloadJob.variant)} · {webDownloadQualityLabel(webDownloadJob)} · {Math.round(webDownloadProgress(webDownloadJob) * 100)}%
                    {' · '}{formatBytes(webDownloadJob.downloaded_bytes)}
                    {(webDownloadJob.total_bytes ?? 0) > 0 ? ` / ${formatBytes(webDownloadJob.total_bytes ?? 0)}` : ''}
                    {webDownloadJob.speed > 0 ? ` · ${formatBytes(webDownloadJob.speed, true)}` : ''}
                    {(webDownloadJob.eta ?? 0) > 0 ? ` · 剩余 ${formatEta(webDownloadJob.eta ?? 0)}` : ''}
                  </span>
                </div>
              ) : null}
              {webDownloadWarning ? <span className="work-web-download-warning" role="status">{webDownloadWarning}</span> : null}
              {webDownloadConfirm ? (
                <div className="work-web-download-confirm" role="group" aria-label="重复下载确认">
                  <span>{webDownloadConfirm}，仍要下载？</span>
                  <Button type="button" size="small" variant="primary" onClick={() => { setWebDownloadConfirm(''); submitWebDownload() }}>仍要下载</Button>
                  <Button type="button" size="small" variant="ghost" onClick={() => setWebDownloadConfirm('')}>取消</Button>
                </div>
              ) : null}
              <div className="work-web-download-actions">
                <Button
                  type="button"
                  size="small"
                  variant="primary"
                  onClick={() => void requestWebDownload()}
                  disabled={!webDownloadReady || webDownloadsQuery.isError || webDownloadIntentActive || startWebDownload.isPending || checkingWebHistory}
                >
                  {startWebDownload.isPending || checkingWebHistory ? <LoaderCircle className="spin" aria-hidden="true" /> : <Download aria-hidden="true" />}
                  {checkingWebHistory ? '检查记录' : startWebDownload.isPending ? '正在添加' : '开始 Web 下载'}
                </Button>
                {activeWebDownloadJob?.can_cancel ? (
                  <Button
                    type="button"
                    size="small"
                    variant="secondary"
                    onClick={() => cancelWebDownload.mutate(activeWebDownloadJob.job_id)}
                    disabled={webDownloadsQuery.isError || cancelWebDownload.isPending}
                  >
                    {cancelWebDownload.isPending ? <LoaderCircle className="spin" aria-hidden="true" /> : <X aria-hidden="true" />}
                    {cancelWebDownload.isPending ? '正在取消' : '取消下载'}
                  </Button>
                ) : null}
                {webDownloadJob?.can_retry ? (
                  <Button
                    type="button"
                    size="small"
                    variant="secondary"
                    onClick={() => continueWebDownload.mutate(webDownloadJob.job_id)}
                    disabled={!webDownloadReady || webDownloadsQuery.isError || continueWebDownload.isPending}
                  >
                    {continueWebDownload.isPending ? <LoaderCircle className="spin" aria-hidden="true" /> : <RotateCcw aria-hidden="true" />}
                    {continueWebDownload.isPending ? '正在继续' : '继续下载'}
                  </Button>
                ) : null}
                {webDownloadReselection ? (
                  <Button
                    type="button"
                    size="small"
                    variant="secondary"
                    onClick={() => prepareMagnetReplacement.mutate(webDownloadReselection)}
                    disabled={prepareMagnetReplacement.isPending}
                  >
                    {prepareMagnetReplacement.isPending ? <LoaderCircle className="spin" aria-hidden="true" /> : <RadioTower aria-hidden="true" />}
                    {prepareMagnetReplacement.isPending ? '正在探测磁链' : '探测可用磁链'}
                  </Button>
                ) : null}
                {webDownloadsQuery.isError || (webDownloadsQuery.data?.configured && !webDownloadsQuery.data.ok) ? (
                  <Button type="button" size="small" variant="ghost" onClick={() => void webDownloadsQuery.refetch()} disabled={webDownloadsQuery.isFetching}>
                    <RotateCcw className={webDownloadsQuery.isFetching ? 'spin' : ''} aria-hidden="true" />
                    重新检查
                  </Button>
                ) : null}
                {webDownloadJob || webDownloadIntent ? (
                  <Button type="button" size="small" variant="ghost" onClick={() => navigate('/downloads?view=web')}>
                    {webDownloadJob?.status === 'completed' && webDownloadJob.archive_status === 'missing'
                      ? '前往下载页处理'
                      : '查看 Web 任务'}
                  </Button>
                ) : null}
              </div>
              {webDownloadJob ? (
                <span className="probe-announcement" role="status" aria-live="polite" aria-atomic="true">
                  {`${webDownloadJob.code} ${webDownloadVariantLabel(webDownloadJob.variant)}：${webDownloadPresentation?.label ?? webDownloadStatusLabel(webDownloadJob.status)}，${webDownloadQualityLabel(webDownloadJob)}`}
                </span>
              ) : null}
            </div>
          ) : null}
        </div>
      </div>

      <section className="work-detail-section source-media-section" aria-labelledby="work-sources-title">
        <div className="detail-section-heading">
          <div>
            <h2 id="work-sources-title">站点截图</h2>
            <span>按来源核对详情与真实截图</span>
          </div>
        </div>

        {work.sources.length ? (
          <>
            <div className="source-tabs" role="tablist" aria-label="作品来源">
              {work.sources.map((source, index) => {
                const selected = source.source_id === activeSource?.source_id
                return (
                  <button
                    type="button"
                    role="tab"
                    aria-label={sourceName(source.source_id, sites)}
                    id={`work-source-tab-${source.source_id}`}
                    aria-selected={selected}
                    aria-controls="work-source-panel"
                    tabIndex={selected ? 0 : -1}
                    onClick={() => activateSource(source)}
                    onKeyDown={(event) => handleTabKeyDown(event, index)}
                    key={source.source_id}
                  >
                    {sourceName(source.source_id, sites)}
                    <span>{sourceScreenshots.get(source.source_id)?.length ?? 0}</span>
                  </button>
                )
              })}
            </div>

            {activeSource ? (
              <div className="source-panel" id="work-source-panel" role="tabpanel" aria-labelledby={`work-source-tab-${activeSource.source_id}`}>
                <div className="source-panel-toolbar">
                  <div>
                    <StatusBadge tone={activeSource.parse_status === 'error' ? 'danger' : activeSource.parse_status === 'resolved' || activeSource.parse_status === 'complete' ? 'success' : 'info'}>
                      {parseStatusLabel(activeSource.parse_status)}
                    </StatusBadge>
                    <span>{activeSource.release_date || '日期未知'}</span>
                  </div>
                  {activeSource.detail_url ? (
                    <a className="button button-secondary button-small" href={activeSource.detail_url} target="_blank" rel="noopener noreferrer" aria-label={`在 ${sourceName(activeSource.source_id, sites)} 打开`}>
                      站点详情
                      <ExternalLink aria-hidden="true" />
                    </a>
                  ) : null}
                </div>
                {activeSourceError ? <InlineNotice tone="warning">{serviceErrorMessage(activeSourceError, '该来源的部分详情暂时无法加载')}</InlineNotice> : null}
                {hasSourceDetails(activeSource.details) ? (
                  <SourceDetailsView
                    details={activeSource.details}
                    fieldSources={activeSource.field_sources}
                    hrefFor={(item) => relatedSearchHref(item, activeSource.source_id)}
                  />
                ) : null}

                <WorkMediaGallery
                  sourceId={activeSource.source_id}
                  sourceLabel={sourceName(activeSource.source_id, sites)}
                  images={images}
                  sites={sites}
                  retryToken={workQuery.dataUpdatedAt}
                  imageStates={activeImageStates}
                  onImageState={markImageState}
                  onOpen={setLightboxIndex}
                />
              </div>
            ) : null}
          </>
        ) : <EmptyState className="compact-empty" title="暂无站点详情" />}
      </section>

      <section className="work-detail-section" aria-labelledby="work-magnets-title">
        <div className="detail-section-heading">
          <div>
            <h2 id="work-magnets-title">完整磁链</h2>
            <span>{work.magnets.length} 条去重资源</span>
          </div>
          <div className="detail-section-actions">
            <Button
              type="button"
              size="small"
              variant="secondary"
              disabled={!work.magnets.length || probeActive || selectionActive}
              onClick={() => void startMagnetProbe()}
            >
              {probeActive ? <LoaderCircle className="spin" aria-hidden="true" /> : <RadioTower aria-hidden="true" />}
              {probeActive ? '探测中' : probe ? '重新探测' : '探测做种'}
            </Button>
            <Button
              type="button"
              size="small"
              variant="primary"
              disabled={!work.magnets.length || probeActive || selectionActive}
              onClick={() => void startMagnetSelection()}
            >
              {selectionActive ? <LoaderCircle className="spin" aria-hidden="true" /> : <WandSparkles aria-hidden="true" />}
              {selectionActive ? '选种中' : selection ? '重新选种' : '智能选种'}
            </Button>
          </div>
        </div>
        {probe ? <MagnetProbeProgress probe={probe} /> : null}
        {selection ? <MagnetSelectionProgress selection={selection} /> : null}
        {probeStartError || probeQuery.isError || probe?.error ? (
          <InlineNotice tone="danger" role="alert">
            {probeStartError || (probeQuery.error as Error | null)?.message || probe?.error}
          </InlineNotice>
        ) : null}
        {selectionStartError || selectionQuery.isError || selection?.error ? (
          <InlineNotice tone="danger" role="alert">
            {selectionStartError || (selectionQuery.error as Error | null)?.message || selection?.error}
          </InlineNotice>
        ) : null}
        <WorkMagnetList
          magnets={work.magnets}
          sites={sites}
          states={downloadStates}
          probeItems={probeItems}
          selectionItems={selectionItems}
          onDownload={(magnet) => void addDownload(magnet)}
          onOpenDownloads={() => navigate('/downloads')}
        />
      </section>

      <dialog
        ref={dialogRef}
        className="image-lightbox"
        aria-label="图片查看器"
        onClose={() => setLightboxIndex(null)}
        onCancel={() => setLightboxIndex(null)}
        onKeyDown={handleDialogKeyDown}
        onClick={(event) => {
          if (event.target === event.currentTarget) setLightboxIndex(null)
        }}
      >
        {lightboxImage && activeSource && lightboxIndex !== null ? (
          <div className="lightbox-content">
            <div className="lightbox-toolbar">
              <span>{lightboxIndex + 1} / {images.length}</span>
              <IconButton label="关闭图片查看器" onClick={() => setLightboxIndex(null)} autoFocus>
                <X aria-hidden="true" />
              </IconButton>
            </div>
            <div className="lightbox-stage">
              <IconButton label="上一张" onClick={() => moveLightbox(-1)} disabled={images.length <= 1}>
                <ChevronLeft aria-hidden="true" />
              </IconButton>
              <StableImage
                src={coverImageUrl(activeSource.source_id, lightboxImage.url, sites)}
                alt={`${sourceName(activeSource.source_id, sites)} 截图 ${lightboxIndex + 1}`}
                retryToken={workQuery.dataUpdatedAt}
                referrerPolicy="no-referrer"
              />
              <IconButton label="下一张" onClick={() => moveLightbox(1)} disabled={images.length <= 1}>
                <ChevronRight aria-hidden="true" />
              </IconButton>
            </div>
          </div>
        ) : null}
      </dialog>
    </div>
  )
}
