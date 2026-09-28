import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowLeft, ChevronLeft, ChevronRight, FileImage, FilePenLine, Library, RefreshCw, RotateCcw, ScanLine, Search, ShieldCheck, WandSparkles, X } from 'lucide-react'
import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'

import { useToast } from '../components/ToastProvider'
import { Button, EmptyState, IconButton, InlineNotice, PageHeader, SkeletonRows, StatusBadge } from '../components/ui'
import { ApiError, api } from '../lib/api'
import { formatDateTime } from '../lib/format'
import { catalogHistoryQueryError } from '../lib/historyQuery'
import { serviceErrorMessage } from '../lib/presentation'
import type {
  MediaMetadataCompletePayload,
  MediaMetadataJob,
  MediaMetadataKind,
  MediaMetadataMigrationPayload,
  MediaMetadataMigrationPreviewPayload,
  MediaMetadataMigrationResult,
  MediaMetadataReview,
  MediaMetadataStatus,
} from '../types'
import { MetadataReviewPanel } from './MetadataReviewPanel'
import { t } from '../lib/i18n'

import '../styles/metadata.css'

const ACTIVE_STATUSES = new Set<MediaMetadataStatus>(['queued', 'running', 'retry'])
const METADATA_PAGE_SIZE = 50
const metadataHistoryFilters = [
  { value: 'all', label: t('全部状态') },
  { value: 'waiting_media', label: t('等待媒体') },
  { value: 'queued', label: t('排队中') },
  { value: 'running', label: t('补全中') },
  { value: 'retry', label: t('等待重试') },
  { value: 'completed', label: t('已完成') },
  { value: 'failed', label: t('失败') },
]
const DISPLAY_CODE_PATTERN = /^[A-Z0-9]+(?:[-._][A-Z0-9]+)*$/
const QUALITY_CODE_PATTERN = /^(?:(?:[248]K|720P|1080P|2160P|4320P|SD|HD|FHD|UHD|VR|HDR|HEVC|H26[45]|X26[45]|AV1|\d{2,3}FPS))+$/

const statusLabels: Record<MediaMetadataStatus, string> = {
  waiting_media: t('等待媒体'),
  queued: t('排队中'),
  running: t('补全中'),
  retry: t('等待重试'),
  completed: t('已完成'),
  failed: t('失败'),
}

const kindLabels: Record<MediaMetadataKind, string> = {
  qb: 'BT',
  web: 'Web',
  manual: t('手动'),
}

const assetLabels: Record<string, string> = {
  nfo: 'NFO',
  poster: t('封面'),
  fanart: t('背景'),
  backdrop: t('背景副本'),
  landscape: t('横图'),
  thumb: t('缩略图'),
}

const assetStatusLabels: Record<string, string> = {
  generated: t('已生成'),
  existing: t('已存在'),
  missing: t('缺失'),
  conflict: t('冲突'),
}

const migrationStatusLabels: Record<string, string> = {
  ready: t('待迁移'),
  migrated: t('已迁移'),
  current: t('已是最新'),
  missing: t('NFO 缺失'),
  invalid: t('结构或番号不匹配'),
  unverified: t('缺少任务追踪记录'),
  failed: t('迁移失败'),
}

const migrationItemErrorLabels: Record<string, string> = {
  'nfo inspection failed': t('NFO 检查失败'),
  'nfo migration failed': t('NFO 迁移失败，原文件已保留'),
  'nfo changed after preview or migration failed': t('NFO 在确认后发生变化或迁移失败，请重新检查'),
}

function normalizeMetadataCode(value: string): string | null {
  const normalized = value.normalize('NFKC').trim().toUpperCase()
  if (!normalized || normalized.length > 40 || !DISPLAY_CODE_PATTERN.test(normalized)) return null
  const canonical = normalized.replace(/[-._]/g, '')
  const fc2Match = canonical.match(/^FC2PPV(\d{2,9})$/)
  if (fc2Match) return `FC2-PPV-${fc2Match[1]}`
  const alphaCount = canonical.match(/[A-Z]/g)?.length ?? 0
  if (
    canonical.length < 3
    || !alphaCount
    || !/\d/.test(canonical)
    || QUALITY_CODE_PATTERN.test(canonical)
    || (!/[-._]/.test(normalized) && alphaCount < 2)
  ) return null
  return normalized
}

function libraryReturnPath(value: string | null): string | null {
  if (!value || !value.startsWith('/library') || value.startsWith('//')) return null
  try {
    const parsed = new URL(value, window.location.origin)
    if (parsed.origin !== window.location.origin || parsed.pathname !== '/library') return null
    return `${parsed.pathname}${parsed.search}${parsed.hash}`
  } catch {
    return null
  }
}

type MetadataWriteAction = 'scan' | 'preview' | 'migrate' | 'retry'

function metadataActionErrorMessage(error: unknown, action: MetadataWriteAction): string {
  if (error instanceof ApiError) {
    if (error.status === 404) {
      return action === 'retry'
        ? t('元数据任务不存在或已被清理，请刷新列表后重试。')
        : t('未找到该番号对应的媒体文件，请确认文件已入库后重试。')
    }
    if (error.status === 409) {
      return action === 'preview' || action === 'migrate'
        ? t('媒体库内容已变化或检查结果已过期，请重新检查后再迁移。')
        : t('任务状态已变化，请刷新页面后重试。')
    }
    if (error.status === 503) return t('元数据服务暂不可用，请稍后重试。')
  }
  return t('操作未完成，请稍后重试。')
}

function metadataReviewOpenErrorMessage(error: unknown): string {
  if (error instanceof ApiError) {
    if (error.status === 404) return t('媒体文件不存在或已从归档中删除，请刷新媒体库后重试。')
    if (error.status === 409) return t('媒体文件状态已变化，请刷新任务后重新打开。')
    if (error.status === 422) return t('该任务的番号或媒体路径无法用于审校。')
    if (error.status === 503) return t('元数据审校服务暂不可用，请稍后重试。')
  }
  return t('无法打开元数据审校，请稍后重试。')
}

function migrationItemErrorMessage(error: string | undefined): string | null {
  const cleanError = error?.trim()
  if (!cleanError) return null
  const translated = migrationItemErrorLabels[cleanError.toLowerCase()]
  if (translated) return translated
  return t('处理失败，请重新检查该文件')
}

function migrationItemStatusLabel(item: MediaMetadataMigrationResult): string {
  if (item.status === 'ready' && item.provenance === 'tracked_existing') {
    return t('待迁移（历史已追踪）')
  }
  return migrationStatusLabels[item.status] || item.status
}

function MigrationItemList({
  items,
  label,
}: {
  items: MediaMetadataMigrationResult[]
  label: string
}) {
  return (
    <ul className="metadata-migration-list" aria-label={label}>
      {items.map((item) => {
        const itemError = migrationItemErrorMessage(item.error)
        return (
          <li key={`${item.code}:${item.nfo_path}:${item.status}`}>
            <span>{item.code}</span>
            <span className="metadata-migration-item-detail">
              <span>{migrationItemStatusLabel(item)}</span>
              {itemError ? <span className="metadata-migration-item-error">{itemError}</span> : null}
              <code>{item.nfo_path}</code>
            </span>
          </li>
        )
      })}
    </ul>
  )
}

function statusTone(status: MediaMetadataStatus) {
  if (status === 'completed') return 'success' as const
  if (status === 'failed') return 'danger' as const
  if (status === 'running') return 'info' as const
  if (status === 'retry') return 'warning' as const
  return 'neutral' as const
}

function assetSummary(job: MediaMetadataJob) {
  const entries = Object.entries(job.assets)
  const published = entries.filter(([, asset]) => ['generated', 'existing'].includes(asset.status))
  const labelFor = (name: string) => {
    if (name.toLowerCase().endsWith('.nfo')) return 'NFO'
    const stem = name.replace(/\.[^.]+$/, '').toLowerCase()
    return assetLabels[stem] || name
  }
  return {
    count: published.length,
    searchCardOnly: published.some(([, asset]) => asset.detail === 'search_card'),
    labels: published.map(([name]) => labelFor(name)).join(t('、')),
    paths: entries
      .map(([name, asset]) => `${name}: ${assetStatusLabels[asset.status] || asset.status}`)
      .join('\n'),
  }
}

export function MetadataPage() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const location = useLocation()
  const navigate = useNavigate()
  const [routeParams] = useSearchParams()
  const routeCode = normalizeMetadataCode(routeParams.get('code') || '') || ''
  const returnPath = libraryReturnPath(routeParams.get('return'))
  const returnState = location.state as { libraryReturnPath?: unknown } | null
  const canPopToLibrary = Boolean(returnPath && returnState?.libraryReturnPath === returnPath)
  const [code, setCode] = useState(routeCode)
  const [codeTouched, setCodeTouched] = useState(false)
  const [migrationScope, setMigrationScope] = useState<string | null | undefined>(null)
  const [migrationPreview, setMigrationPreview] = useState<MediaMetadataMigrationPreviewPayload | null>(null)
  const [migrationReport, setMigrationReport] = useState<MediaMetadataMigrationPayload | null>(null)
  const [historyFilter, setHistoryFilter] = useState('all')
  const [historyQueryInput, setHistoryQueryInput] = useState('')
  const [historyQuery, setHistoryQuery] = useState('')
  const [historyPage, setHistoryPage] = useState(0)
  const historyQueryError = catalogHistoryQueryError(historyQueryInput)
  const [metadataReview, setMetadataReview] = useState<MediaMetadataReview | null>(null)
  const cleanCode = code.trim()
  const normalizedCode = normalizeMetadataCode(code)
  const codeInvalid = codeTouched && Boolean(cleanCode) && !normalizedCode

  useEffect(() => {
    setCode(routeCode)
    setCodeTouched(false)
    setMigrationScope(null)
    setMigrationPreview(null)
    setMigrationReport(null)
  }, [routeCode])

  const tasks = useQuery({
    queryKey: ['media-metadata', historyFilter, historyQuery, historyPage],
    queryFn: () => api.mediaMetadata({
      filter: historyFilter,
      query: historyQuery || undefined,
      limit: METADATA_PAGE_SIZE,
      offset: historyPage * METADATA_PAGE_SIZE,
    }),
    retry: false,
    refetchInterval: (query) => {
      const payload = query.state.data
      const jobs = payload?.jobs ?? []
      return (payload?.summary?.running ?? 0) > 0 || jobs.some((job) => ACTIVE_STATUSES.has(job.status)) ? 3_000 : 12_000
    },
    refetchIntervalInBackground: false,
  })

  const scan = useMutation({
    mutationFn: (requestedCode?: string) => api.scanMediaMetadata(requestedCode),
    onMutate: () => {
      setMigrationScope(null)
      setMigrationPreview(null)
      setMigrationReport(null)
    },
    onSuccess: (payload, requestedCode) => {
      const scope = requestedCode ? requestedCode : t('媒体库')
      toast.push(payload.queued ? t('{scope} 已加入 {queued} 个补全任务', { scope, queued: payload.queued }) : t('{scope} 没有待补全项目', { scope }), 'success')
      if (requestedCode) {
        setCode('')
        setCodeTouched(false)
      }
      void queryClient.invalidateQueries({ queryKey: ['media-metadata'] })
    },
    onError: (error) => toast.push(metadataActionErrorMessage(error, 'scan'), 'error'),
  })

  const previewTitles = useMutation({
    mutationFn: (requestedCode?: string) => api.previewMediaMetadataTitles(requestedCode),
    onMutate: () => {
      setMigrationPreview(null)
      setMigrationReport(null)
    },
    onSuccess: (payload, requestedCode) => {
      setMigrationScope(requestedCode)
      setMigrationPreview(payload)
      if (!payload.ready) {
        toast.push(
          !payload.scanned
            ? t('没有可迁移的已追踪 NFO')
            : payload.failed || payload.skipped
            ? t('旧 NFO 检查完成，存在需要人工确认的项目')
            : t('已追踪 NFO 均为最新'),
          payload.scanned && !payload.failed && !payload.skipped ? 'success' : 'info',
        )
      }
    },
    onError: (error) => {
      setMigrationScope(null)
      setMigrationPreview(null)
      toast.push(metadataActionErrorMessage(error, 'preview'), 'error')
    },
  })

  const migrateTitles = useMutation({
    mutationFn: (previewId: string) => api.migrateMediaMetadataTitles(previewId),
    onMutate: () => setMigrationReport(null),
    onSuccess: (payload) => {
      setMigrationScope(null)
      setMigrationPreview(null)
      setMigrationReport(payload)
      if (payload.failed) {
        toast.push(t('旧 NFO 迁移有 {failed} 个失败项目', { failed: payload.failed }), 'error')
      } else if (payload.skipped) {
        toast.push(
          t('已迁移 {migrated} 个旧 NFO，{skipped} 个需要检查', { migrated: payload.migrated, skipped: payload.skipped }),
          'info',
        )
      } else if (payload.migrated) {
        toast.push(t('已迁移 {migrated} 个旧 NFO', { migrated: payload.migrated }), 'success')
      } else if (payload.current) {
        toast.push(t('已追踪 NFO 均为最新'), 'success')
      } else {
        toast.push(t('媒体库中没有可迁移的已追踪 NFO'), 'info')
      }
    },
    onError: (error) => {
      // The server may have consumed the snapshot even when the client sees an error.
      setMigrationScope(null)
      setMigrationPreview(null)
      toast.push(metadataActionErrorMessage(error, 'migrate'), 'error')
    },
  })

  const [completeReport, setCompleteReport] = useState<MediaMetadataCompletePayload | null>(null)
  const completeAll = useMutation({
    mutationFn: api.completeMediaMetadata,
    onSuccess: (payload) => {
      setCompleteReport(payload)
      void queryClient.invalidateQueries({ queryKey: ['media-metadata'] })
      toast.push(t('已加入 {queued} 项补全任务，重新尝试 {retried} 项', { queued: payload.queued, retried: payload.retried }), 'success')
    },
    onError: (error) => toast.push(metadataActionErrorMessage(error, 'scan'), 'error'),
  })
  const retry = useMutation({
    mutationFn: (jobId: string) => api.retryMediaMetadata(jobId),
    onMutate: () => {
      setMigrationScope(null)
      setMigrationPreview(null)
      setMigrationReport(null)
    },
    onSuccess: () => {
      toast.push(t('元数据任务已重新排队'), 'success')
      void queryClient.invalidateQueries({ queryKey: ['media-metadata'] })
    },
    onError: (error) => toast.push(metadataActionErrorMessage(error, 'retry'), 'error'),
  })

  const openReview = useMutation({
    mutationFn: (job: MediaMetadataJob) => {
      if (!job.relative_media_path) throw new Error('media path is unavailable')
      return api.openMediaMetadataReview(job.code, job.relative_media_path)
    },
    onMutate: () => {
      setMigrationScope(null)
      setMigrationPreview(null)
      setMigrationReport(null)
    },
    onSuccess: (payload) => setMetadataReview(payload.review),
    onError: (error) => toast.push(metadataReviewOpenErrorMessage(error), 'error'),
  })

  const summary = useMemo(() => {
    if (tasks.data?.summary) return tasks.data.summary
    const jobs = tasks.data?.jobs ?? []
    return {
      total: jobs.length,
      waiting: jobs.filter((job) => ['waiting_media', 'queued', 'retry'].includes(job.status)).length,
      running: jobs.filter((job) => job.status === 'running').length,
      completed: jobs.filter((job) => job.status === 'completed').length,
      failed: jobs.filter((job) => job.status === 'failed').length,
    }
  }, [tasks.data?.jobs, tasks.data?.summary])
  const historyCount = tasks.data?.count ?? tasks.data?.jobs.length ?? 0
  const historyPageCount = Math.max(1, Math.ceil(historyCount / METADATA_PAGE_SIZE))
  const historyHasNext = tasks.data?.has_more ?? historyPage + 1 < historyPageCount

  useEffect(() => {
    if (!tasks.isLoading && historyPage >= historyPageCount) {
      setHistoryPage(Math.max(0, historyPageCount - 1))
    }
  }, [historyPage, historyPageCount, tasks.isLoading])

  function submitCode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (!cleanCode || scan.isPending || previewTitles.isPending || migrateTitles.isPending) return
    if (!normalizedCode) {
      setCodeTouched(true)
      return
    }
    setMigrationScope(null)
    scan.mutate(normalizedCode)
  }

  function requestMigration() {
    if (cleanCode && !normalizedCode) {
      setCodeTouched(true)
      return
    }
    setMigrationReport(null)
    previewTitles.mutate(normalizedCode || undefined)
  }

  const metadataWritePending = scan.isPending || previewTitles.isPending || migrateTitles.isPending || retry.isPending || completeAll.isPending
  const metadataWriteLocked = metadataWritePending || migrationPreview !== null
  const metadataPageLocked = metadataWriteLocked || openReview.isPending || metadataReview !== null
  const migrationPreviewReviewItems = migrationPreview?.results.filter(
    (item) => item.status === 'ready'
      ? item.provenance !== 'generated'
      : item.status !== 'current',
  ) ?? []
  const migrationPreviewGeneratedItems = migrationPreview?.results.filter(
    (item) => item.status === 'ready' && item.provenance === 'generated',
  ) ?? []
  const migrationIssues = migrationReport?.results.filter(
    (item) => ['missing', 'invalid', 'unverified', 'failed'].includes(item.status),
  ) ?? []

  const responseUnavailable = tasks.data && !tasks.data.ok

  return (
    <div className="page metadata-page">
      <PageHeader
        title={t('元数据补全')}
        description={t('媒体库 NFO 与图片发布任务')}
        actions={(
          <>
            {returnPath ? (
              canPopToLibrary ? (
                <Button type="button" variant="ghost" onClick={() => navigate(-1)}>
                  <ArrowLeft aria-hidden="true" />
                  {t('返回媒体库')}
                </Button>
              ) : (
                <Link className="button button-ghost button-normal" to={returnPath}>
                  <ArrowLeft aria-hidden="true" />
                  {t('返回媒体库')}
                </Link>
              )
            ) : null}
            <IconButton label={t('刷新元数据任务')} onClick={() => void tasks.refetch()} disabled={tasks.isFetching}>
              <RefreshCw className={tasks.isFetching ? 'spin' : ''} aria-hidden="true" />
            </IconButton>
          </>
        )}
      />

      <section className="metadata-controls" aria-label={t('补全元数据与海报')}>
        <div className="metadata-primary-action">
          <Button
            type="button"
            variant="primary"
            onClick={() => {
              setMigrationScope(null)
              setMigrationPreview(null)
              setCompleteReport(null)
              completeAll.mutate()
            }}
            disabled={metadataPageLocked}
          >
            <WandSparkles aria-hidden="true" />
            {completeAll.isPending ? t('正在补全') : t('一键补全')}
          </Button>
          <span>
            {t('检查整个媒体库（包括手动放入的文件夹），为缺少 NFO、封面或背景图的作品自动补全，并重新尝试之前失败的任务。 默认来源取不到资料时会自动换用其他站点。')}
          </span>
        </div>
        {completeReport ? (
          <InlineNotice tone={completeReport.unidentified ? 'warning' : 'success'} role="status">
            <strong>{t('一键补全已开始')}</strong>
            <span>
              {t('新加入 {queued} 项，重新尝试 {retried} 项', { queued: completeReport.queued, retried: completeReport.retried })}
              {completeReport.skipped ? t('，{skipped} 项因下载任务已删除而跳过', { skipped: completeReport.skipped }) : ''}{t('。进度会在下方任务列表中更新。')}
            </span>
            {completeReport.unidentified ? (
              <span>
                {t('有 {unidentified} 个视频无法从文件夹或文件名识别番号，暂时无法补全：', { unidentified: completeReport.unidentified })}
                {completeReport.unidentified_examples.join(t('、'))}
                {completeReport.unidentified > completeReport.unidentified_examples.length ? t(' 等') : ''}{t('。 将文件夹或视频文件重命名为番号（例如“番号/番号.mp4”）后再次一键补全即可。')}
              </span>
            ) : null}
          </InlineNotice>
        ) : null}
        <form className="metadata-command-bar" aria-label={t('创建元数据补全任务')} onSubmit={submitCode}>
          <label className="metadata-code-field">
            <span>{t('指定作品番号')}</span>
            <input
              value={code}
              onChange={(event) => {
                setCode(event.target.value)
                setMigrationScope(null)
                setMigrationPreview(null)
                setMigrationReport(null)
              }}
              onBlur={() => setCodeTouched(true)}
              placeholder={t('请输入番号')}
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              maxLength={40}
              aria-invalid={codeInvalid || undefined}
              aria-describedby={codeInvalid ? 'metadata-code-error' : undefined}
              disabled={metadataWritePending}
            />
          </label>
          <Button type="submit" variant="primary" disabled={!cleanCode || metadataPageLocked}>
            <FileImage aria-hidden="true" />
            {scan.isPending && scan.variables ? t('补全中') : t('补全番号')}
          </Button>
          {codeInvalid ? (
            <span className="metadata-code-error" id="metadata-code-error" role="alert">
              {t('请输入合法的作品番号')}
            </span>
          ) : null}
        </form>
        <details className="metadata-advanced-tools">
          <summary>{t('高级工具')}</summary>
          <div>
            <Button
              type="button"
              size="small"
              onClick={() => {
                setMigrationScope(null)
                setMigrationPreview(null)
                scan.mutate(undefined)
              }}
              disabled={metadataPageLocked}
            >
              <ScanLine aria-hidden="true" />
              {scan.isPending && !scan.variables ? t('扫描中') : t('仅扫描缺失项')}
            </Button>
            <Button type="button" size="small" onClick={requestMigration} disabled={metadataPageLocked}>
              <FilePenLine aria-hidden="true" />
              {previewTitles.isPending ? t('检查中') : t('迁移旧 NFO')}
            </Button>
            <span>{t('“仅扫描缺失项”不会重试失败任务；“迁移旧 NFO”为旧版本生成的 NFO 标题补上番号前缀，修改前自动备份。')}</span>
          </div>
        </details>
        <p className="metadata-preservation-note">
          <ShieldCheck aria-hidden="true" />
          <span>
            <strong>{t('日常补全保留已有文件。')}</strong>
            {t('仅补齐缺失文件；迁移旧 NFO 只更新符合条件的已追踪文件，修改前备份，图片不变。')}
          </span>
        </p>
        {migrationPreview ? (
          <InlineNotice
            tone={migrationPreview.failed || migrationPreview.skipped || migrationPreview.tracked_existing ? 'warning' : 'info'}
            role="status"
          >
            <strong>{migrationScope ? t('迁移 {migrationScope} 的旧 NFO', { migrationScope }) : t('迁移媒体库旧 NFO')}</strong>
            <span>{t('已检查 {scanned}，待迁移 {ready}，已是最新 {current}，跳过 {skipped}，失败 {failed}。', { scanned: migrationPreview.scanned, ready: migrationPreview.ready, current: migrationPreview.current, skipped: migrationPreview.skipped, failed: migrationPreview.failed })}</span>
            <span>
              {t('确认后只更新本次检查中内容哈希未变化、NFO 结构与番号身份一致的已追踪文件；更新前逐文件备份，图片保持不变。')}
            </span>
            {migrationPreview.tracked_existing ? (
              <span>{t('其中 {tracked_existing} 项属于历史已追踪文件，原任务未记录为本工具生成。', { tracked_existing: migrationPreview.tracked_existing })}</span>
            ) : null}
            {migrationPreviewReviewItems.length ? (
              <div className="metadata-migration-issues">
                <strong>{t('迁移前需核对')}</strong>
                <MigrationItemList items={migrationPreviewReviewItems} label={t('迁移前需核对')} />
              </div>
            ) : null}
            {migrationPreviewGeneratedItems.length ? (
              <details className="metadata-migration-details">
                <summary>{t('普通待迁移 {count} 项', { count: migrationPreviewGeneratedItems.length })}</summary>
                <MigrationItemList items={migrationPreviewGeneratedItems} label={t('普通待迁移 NFO')} />
              </details>
            ) : null}
            <div className="metadata-migration-actions">
              {migrationPreview.ready && migrationPreview.preview_id ? (
                <Button
                  type="button"
                  size="small"
                  variant="primary"
                  onClick={() => migrateTitles.mutate(migrationPreview.preview_id as string)}
                  disabled={migrateTitles.isPending}
                >
                  <FilePenLine aria-hidden="true" />
                  {migrateTitles.isPending ? t('迁移中') : t('备份并迁移')}
                </Button>
              ) : null}
              <Button
                type="button"
                size="small"
                variant="ghost"
                onClick={() => {
                  setMigrationScope(null)
                  setMigrationPreview(null)
                }}
                disabled={migrateTitles.isPending}
              >
                {migrationPreview.ready ? t('取消迁移') : t('关闭')}
              </Button>
            </div>
          </InlineNotice>
        ) : null}
        {migrationReport ? (
          <InlineNotice
            tone={migrationReport.failed || migrationReport.skipped ? 'warning' : 'success'}
            role="status"
          >
            <strong>{t('旧 NFO 迁移结果')}</strong>
            <span>{t('检查 {scanned}，更新 {migrated}，已是最新 {current}，跳过 {skipped}，失败 {failed}。', { scanned: migrationReport.scanned, migrated: migrationReport.migrated, current: migrationReport.current, skipped: migrationReport.skipped, failed: migrationReport.failed })}</span>
            {migrationReport.backup_path ? (
              <span className="metadata-backup-path">
                <span>{t('备份路径')}</span>
                <code>{migrationReport.backup_path}</code>
              </span>
            ) : null}
            {migrationIssues.length ? (
              <div className="metadata-migration-issues">
                {migrationIssues.length > 6 ? (
                  <details className="metadata-migration-details">
                    <summary>{t('查看全部 {count} 个需检查项目', { count: migrationIssues.length })}</summary>
                    <MigrationItemList items={migrationIssues} label={t('迁移结果需检查')} />
                  </details>
                ) : (
                  <>
                    <strong>{t('需检查')}</strong>
                    <MigrationItemList items={migrationIssues} label={t('迁移结果需检查')} />
                  </>
                )}
              </div>
            ) : null}
          </InlineNotice>
        ) : null}
      </section>

      {metadataReview ? (
        <MetadataReviewPanel
          initialReview={metadataReview}
          onClose={() => setMetadataReview(null)}
          onPublished={() => {
            void queryClient.invalidateQueries({ queryKey: ['media-metadata'] })
            void queryClient.invalidateQueries({ queryKey: ['media-library'] })
          }}
        />
      ) : null}

      {tasks.data?.ok ? (
        <section className="summary-strip" aria-label={t('元数据任务摘要')}>
          <div><span>{t('任务总数')}</span><strong>{summary.total}</strong></div>
          <div><span>{t('待处理')}</span><strong>{summary.waiting}</strong></div>
          <div><span>{t('补全中')}</span><strong>{summary.running}</strong></div>
          <div><span>{t('已完成')}</span><strong>{summary.completed}</strong></div>
          <div><span>{t('失败')}</span><strong>{summary.failed}</strong></div>
        </section>
      ) : null}

      <section className="downloads-workspace metadata-workspace" aria-label={t('元数据任务列表')}>
        <div className="section-toolbar downloads-toolbar metadata-toolbar">
          <div>
            <StatusBadge tone={tasks.isLoading ? 'info' : tasks.data?.ok ? 'success' : 'warning'}>
              <Library aria-hidden="true" />
              {t('媒体库')}
            </StatusBadge>
            <span className="metadata-library-path" title={tasks.data?.library_path || undefined}>
              {tasks.isLoading ? t('正在读取路径') : tasks.data?.library_path || t('路径不可用')}
            </span>
          </div>
          <span className="polling-label">{t('页面可见且有活动任务时每 3 秒刷新')}</span>
        </div>

        <form
          className="web-download-history-controls metadata-history-controls"
          aria-label={t('筛选元数据任务历史')}
          onSubmit={(event) => {
            event.preventDefault()
            if (historyQueryError) return
            setHistoryPage(0)
            setHistoryQuery(historyQueryInput.trim())
          }}
        >
          <label>
            <span className="sr-only">{t('任务状态')}</span>
            <select
              value={historyFilter}
              onChange={(event) => {
                setHistoryFilter(event.target.value)
                setHistoryPage(0)
              }}
            >
              {metadataHistoryFilters.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}
            </select>
          </label>
          <label className="web-download-history-search">
            <Search aria-hidden="true" />
            <span className="sr-only">{t('搜索番号')}</span>
            <input
              type="search"
              value={historyQueryInput}
              placeholder={t('搜索番号')}
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              maxLength={40}
              onChange={(event) => setHistoryQueryInput(event.target.value)}
              aria-invalid={Boolean(historyQueryError)}
              aria-describedby={historyQueryError ? 'metadata-history-query-error' : undefined}
            />
          </label>
          <Button type="submit" size="small" variant="ghost" disabled={tasks.isFetching || Boolean(historyQueryError)}>{t('筛选')}</Button>
          {historyQueryError ? <span id="metadata-history-query-error" className="field-error" role="alert">{historyQueryError}</span> : null}
          {historyQuery ? (
            <Button
              type="button"
              size="small"
              variant="ghost"
              onClick={() => {
                setHistoryQueryInput('')
                setHistoryQuery('')
                setHistoryPage(0)
              }}
            >
              <X aria-hidden="true" />
              {t('清除搜索')}
            </Button>
          ) : null}
        </form>

        {tasks.isLoading ? <SkeletonRows count={6} /> : null}
        {tasks.isError && !tasks.data ? (
          <EmptyState
            role="alert"
            title={t('无法加载元数据任务')}
            description={(tasks.error as Error).message || t('请检查服务状态后重试')}
            action={(
              <Button onClick={() => void tasks.refetch()} disabled={tasks.isFetching}>
                <RefreshCw className={tasks.isFetching ? 'spin' : ''} aria-hidden="true" />
                {tasks.isFetching ? t('重试中') : t('重新加载')}
              </Button>
            )}
          />
        ) : null}
        {tasks.isError && tasks.data ? (
          <InlineNotice tone="warning" role="status">
            <div>
              <span>{t('自动刷新失败，正在显示上次获取的任务。')}</span>
              <Button size="small" variant="ghost" onClick={() => void tasks.refetch()} disabled={tasks.isFetching}>
                <RefreshCw className={tasks.isFetching ? 'spin' : ''} aria-hidden="true" />
                {t('重试')}
              </Button>
            </div>
          </InlineNotice>
        ) : null}
        {responseUnavailable ? (
          <EmptyState
            role="alert"
            title={t('元数据服务暂不可用')}
            description={serviceErrorMessage(tasks.data.error, t('请稍后重新检测服务状态'))}
            action={<Button onClick={() => void tasks.refetch()}>{t('重新检测')}</Button>}
          />
        ) : null}
        {tasks.data?.ok && !tasks.data.jobs.length ? (
          <EmptyState
            role="status"
            title={historyFilter !== 'all' || historyQuery ? t('没有匹配的元数据任务') : t('还没有元数据任务')}
            description={historyFilter !== 'all' || historyQuery ? t('调整状态或番号筛选条件后重试。') : t('点击“一键补全”后，待补全项目会显示在这里。')}
            action={(
              <Button
                onClick={() => {
                  setMigrationScope(null)
                  setCompleteReport(null)
                  completeAll.mutate()
                }}
                disabled={metadataPageLocked}
              >
                <WandSparkles aria-hidden="true" />
                {completeAll.isPending ? t('正在补全') : t('一键补全')}
              </Button>
            )}
          />
        ) : null}

        {tasks.data?.ok && tasks.data.jobs.length ? (
          <div className="metadata-table" role="table" aria-label={t('元数据补全任务')}>
            <div className="metadata-table-head" role="row">
              <span role="columnheader">{t('任务')}</span>
              <span role="columnheader">{t('状态')}</span>
              <span role="columnheader">{t('媒体文件')}</span>
              <span role="columnheader">{t('已发布')}</span>
              <span role="columnheader">{t('操作')}</span>
            </div>
            {tasks.data.jobs.map((job) => (
              <MetadataRow
                job={job}
                retrying={retry.isPending && retry.variables === job.job_id}
                reviewing={openReview.isPending && openReview.variables?.job_id === job.job_id}
                selected={metadataReview?.relative_media_path === job.relative_media_path}
                disabled={metadataPageLocked}
                onReview={() => openReview.mutate(job)}
                onRetry={() => retry.mutate(job.job_id)}
                key={job.job_id}
              />
            ))}
          </div>
        ) : null}
        {tasks.data?.ok && historyCount > 0 ? (
          <nav className="history-pager" aria-label={t('元数据任务历史分页')}>
            <IconButton
              label={t('上一页')}
              size="small"
              onClick={() => setHistoryPage((page) => Math.max(0, page - 1))}
              disabled={historyPage === 0 || tasks.isFetching}
            >
              <ChevronLeft aria-hidden="true" />
            </IconButton>
            <span>{t('第 {value} / {historyPageCount} 页，共 {historyCount} 条', { value: historyPage + 1, historyPageCount, historyCount })}</span>
            <IconButton
              label={t('下一页')}
              size="small"
              onClick={() => setHistoryPage((page) => page + 1)}
              disabled={!historyHasNext || tasks.isFetching}
            >
              <ChevronRight aria-hidden="true" />
            </IconButton>
          </nav>
        ) : null}
      </section>
    </div>
  )
}

function MetadataRow({
  job,
  retrying,
  reviewing,
  selected,
  disabled,
  onReview,
  onRetry,
}: {
  job: MediaMetadataJob
  retrying: boolean
  reviewing: boolean
  selected: boolean
  disabled: boolean
  onReview: () => void
  onRetry: () => void
}) {
  const assets = assetSummary(job)
  const retryLabel = job.status === 'completed' ? t('检查缺失文件 {code}', { code: job.code }) : t('重试 {code}', { code: job.code })
  return (
    <div className={`metadata-row metadata-stage-${job.status}`} role="row">
      <div className="metadata-name-cell" role="cell">
        <strong>{job.code}</strong>
        <div>
          <StatusBadge>{kindLabels[job.kind]}</StatusBadge>
          <span>{t('更新于 {dateTime}', { dateTime: formatDateTime(job.updated_at) })}</span>
        </div>
        {job.error ? <span className="metadata-job-error">{serviceErrorMessage(job.error, t('元数据处理失败，请稍后重试'))}</span> : null}
        {assets.searchCardOnly ? <span className="metadata-job-note">{t('NFO 仅含搜索卡片数据（详情页需登录），可审校补全')}</span> : null}
      </div>
      <div className="metadata-status-cell" role="cell">
        <StatusBadge tone={statusTone(job.status)}>{statusLabels[job.status]}</StatusBadge>
        <span>
          {job.attempts
            ? t('已尝试 {attempts}{value} 次', { attempts: job.attempts, value: job.max_attempts ? ` / ${job.max_attempts}` : '' })
            : t('尚未执行')}
          {job.status === 'retry' && job.next_attempt_at ? t('，{dateTime} 自动重试', { dateTime: formatDateTime(job.next_attempt_at) }) : ''}
        </span>
      </div>
      <div className="metadata-path-cell" role="cell">
        <strong title={job.relative_media_path || undefined}>{job.relative_media_path || t('等待媒体入库')}</strong>
        <span>{t('创建于 {dateTime}', { dateTime: formatDateTime(job.created_at) })}</span>
      </div>
      <div className="metadata-assets-cell" role="cell" title={assets.paths || undefined}>
        <strong>{t('{count} 项', { count: assets.count })}</strong>
        <span>{assets.labels || t('尚未发布')}</span>
      </div>
      <div className="metadata-row-actions" role="cell">
        {job.relative_media_path ? (
          <IconButton
            label={t('审校 {code}', { code: job.code })}
            onClick={onReview}
            disabled={disabled && !selected}
            aria-pressed={selected}
          >
            <FilePenLine className={reviewing ? 'spin' : ''} aria-hidden="true" />
          </IconButton>
        ) : null}
        {job.can_retry ? (
          <IconButton label={retryLabel} onClick={onRetry} disabled={disabled}>
            <RotateCcw className={retrying ? 'spin' : ''} aria-hidden="true" />
          </IconButton>
        ) : <span aria-hidden="true">-</span>}
      </div>
    </div>
  )
}
