import '../styles/library.css'
import { useMutation, useQuery } from '@tanstack/react-query'
import {
  AlertTriangle,
  Check,
  ChevronDown,
  ChevronLeft,
  ChevronRight,
  CircleHelp,
  FileImage,
  Filter,
  FolderOpen,
  Library,
  RefreshCw,
  Search,
  X,
} from 'lucide-react'
import { type FormEvent, useEffect, useMemo, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'

import { Button, EmptyState, IconButton, InlineNotice, PageHeader, SkeletonRows, StatusBadge } from '../components/ui'
import { ApiError, api } from '../lib/api'
import { webDownloadVariantLabel } from '../lib/webDownloads'
import type {
  MediaLibraryAssetStatus,
  MediaLibraryEntry,
  MediaLibraryListParams,
  MediaLibraryPresence,
} from '../types'

const LIBRARY_PAGE_SIZE = 50
const FILTER_KEYS = [
  'q',
  'actor',
  'maker',
  'tag',
  'series',
  'source',
  'presence',
  'completeness',
  'anomaly',
  'quality',
] as const

type FilterKey = (typeof FILTER_KEYS)[number]
type FilterDraft = Record<FilterKey, string>

const emptyFilters: FilterDraft = {
  q: '',
  actor: '',
  maker: '',
  tag: '',
  series: '',
  source: '',
  presence: '',
  completeness: '',
  anomaly: '',
  quality: '',
}

const presenceOptions = [
  { value: '', label: '全部文件状态' },
  { value: 'present', label: '文件存在' },
  { value: 'missing', label: '文件已删除' },
  { value: 'unknown', label: '存储待确认' },
]

const completenessOptions = [
  { value: '', label: '全部完整度' },
  { value: 'complete', label: '元数据完整' },
  { value: 'incomplete', label: '元数据不完整' },
]

const anomalyOptions = [
  { value: '', label: '全部异常' },
  { value: 'duplicate', label: '重复番号' },
  { value: 'unidentified', label: '无法识别番号' },
  { value: 'missing', label: '文件已删除' },
  { value: 'nfo', label: '缺少或无效 NFO' },
  { value: 'portrait', label: '缺少竖版海报' },
  { value: 'landscape', label: '缺少横版海报' },
]

const qualityOptions = [
  { value: '', label: '全部画质' },
  { value: '2160', label: '2160p 及以上' },
  { value: '1080', label: '1080p' },
  { value: '720', label: '720p' },
  { value: 'sd', label: '低于 720p' },
]

function filtersFromParams(params: URLSearchParams): FilterDraft {
  return Object.fromEntries(
    FILTER_KEYS.map((key) => [key, (params.get(key) || '').trim()]),
  ) as FilterDraft
}

function routePage(params: URLSearchParams): number {
  const raw = Number.parseInt(params.get('page') || '1', 10)
  return Number.isFinite(raw) && raw > 0 ? Math.min(raw, 200_000) : 1
}

function appliedFilters(params: URLSearchParams): MediaLibraryListParams {
  const filters = filtersFromParams(params)
  const request: MediaLibraryListParams = {
    limit: LIBRARY_PAGE_SIZE,
    offset: (routePage(params) - 1) * LIBRARY_PAGE_SIZE,
  }
  if (filters.q) request.query = filters.q
  if (filters.actor) request.actor = filters.actor
  if (filters.maker) request.maker = filters.maker
  if (filters.tag) request.tag = filters.tag
  if (filters.series) request.series = filters.series
  if (filters.source) request.source = filters.source
  if (filters.presence) request.presence = filters.presence as MediaLibraryPresence
  if (filters.completeness) request.completeness = filters.completeness as 'complete' | 'incomplete'
  if (filters.anomaly) request.anomaly = filters.anomaly as NonNullable<MediaLibraryListParams['anomaly']>
  if (filters.quality === '2160') request.min_height = 2160
  if (filters.quality === '1080') {
    request.min_height = 1080
    request.max_height = 2159
  }
  if (filters.quality === '720') {
    request.min_height = 720
    request.max_height = 1079
  }
  if (filters.quality === 'sd') request.max_height = 719
  return request
}

function activeFilterCount(filters: FilterDraft): number {
  return FILTER_KEYS.filter((key) => filters[key]).length
}

function libraryErrorMessage(error: unknown): string {
  if (error instanceof ApiError && error.status === 503) {
    return '媒体库索引暂不可用，请确认媒体目录已挂载后重试。'
  }
  return '无法读取媒体库，请稍后重试。'
}

function sourceLabel(source: string): string {
  if (source === 'nfo') return 'NFO'
  if (source === 'web') return 'Web 下载'
  if (source === 'qb') return 'BT 下载'
  if (source === 'manual') return '手动入库'
  return '文件路径'
}

function qualityLabel(height: number | null): string {
  if (height === null) return '画质未知'
  if (height >= 4320) return '8K'
  if (height >= 2160) return '4K'
  return `${height}p`
}

function listLabel(values: string[], fallback: string): string {
  return values.length ? values.slice(0, 3).join('、') : fallback
}

function assetLabel(name: string, status: MediaLibraryAssetStatus): string {
  if (status === 'present') return `${name} 完整`
  if (status === 'invalid') return `${name} 无效`
  if (status === 'unknown') return `${name} 待确认`
  return `${name} 缺失`
}

function assetTone(status: MediaLibraryAssetStatus) {
  if (status === 'present') return 'success' as const
  if (status === 'invalid') return 'danger' as const
  if (status === 'missing') return 'warning' as const
  return 'neutral' as const
}

function presenceLabel(presence: MediaLibraryPresence): string {
  if (presence === 'present') return '文件存在'
  if (presence === 'missing') return '文件已删除'
  return '存储待确认'
}

function presenceTone(presence: MediaLibraryPresence) {
  if (presence === 'present') return 'success' as const
  if (presence === 'missing') return 'danger' as const
  return 'warning' as const
}

function metadataTarget(entry: MediaLibraryEntry, returnPath: string): string | null {
  if (!entry.code || entry.presence !== 'present') return null
  const params = new URLSearchParams({ code: entry.code, return: returnPath })
  return `/metadata?${params.toString()}`
}

export function LibraryPage() {
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const routeKey = searchParams.toString()
  const page = routePage(searchParams)
  const applied = useMemo(() => appliedFilters(searchParams), [routeKey])
  const appliedDraft = useMemo(() => filtersFromParams(searchParams), [routeKey])
  const [draft, setDraft] = useState<FilterDraft>(appliedDraft)

  useEffect(() => {
    setDraft(appliedDraft)
  }, [appliedDraft])

  const library = useQuery({
    queryKey: ['media-library', routeKey],
    queryFn: () => api.mediaLibrary(applied),
    retry: false,
    staleTime: 15_000,
  })
  const rebuild = useMutation({
    mutationFn: (acceptRootChange: boolean) => api.rebuildMediaLibrary(
      library.data?.revision ?? 0,
      acceptRootChange,
    ),
    onSuccess: () => library.refetch(),
  })

  const count = library.data?.count ?? 0
  const pageCount = Math.max(1, Math.ceil(count / LIBRARY_PAGE_SIZE))
  const hasFilters = activeFilterCount(appliedDraft) > 0
  const advancedCount = FILTER_KEYS
    .filter((key) => !['q', 'presence', 'completeness', 'anomaly'].includes(key))
    .filter((key) => draft[key]).length
  const returnPath = `${location.pathname}${location.search}`

  useEffect(() => {
    if (!library.data?.ok || library.isFetching || page <= pageCount) return
    const next = new URLSearchParams(searchParams)
    if (pageCount === 1) next.delete('page')
    else next.set('page', String(pageCount))
    setSearchParams(next, { replace: true })
  }, [library.data?.ok, library.isFetching, page, pageCount, searchParams, setSearchParams])

  function updateDraft(key: FilterKey, value: string) {
    setDraft((current) => ({ ...current, [key]: value }))
  }

  function submitFilters(event: FormEvent) {
    event.preventDefault()
    const next = new URLSearchParams()
    FILTER_KEYS.forEach((key) => {
      const value = draft[key].trim()
      if (value) next.set(key, value)
    })
    setSearchParams(next)
  }

  function clearFilters() {
    setDraft(emptyFilters)
    setSearchParams({})
  }

  function goToPage(nextPage: number) {
    const next = new URLSearchParams(searchParams)
    if (nextPage <= 1) next.delete('page')
    else next.set('page', String(nextPage))
    setSearchParams(next)
  }

  const unavailable = library.data && !library.data.ok
  const indexUnknown = library.data?.index_state === 'unknown'

  return (
    <div className="page library-page">
      <PageHeader
        title="媒体库"
        description="本地作品、元数据完整度与文件状态"
        actions={(
          <IconButton
            label="重建媒体库索引"
            onClick={() => rebuild.mutate(false)}
            disabled={library.isFetching || rebuild.isPending}
          >
            <RefreshCw className={library.isFetching || rebuild.isPending ? 'spin' : ''} aria-hidden="true" />
          </IconButton>
        )}
      />

      <section className="library-workspace" aria-label="媒体库作品">
        <form className="library-filter-form" role="search" onSubmit={submitFilters}>
          <div className="library-filter-primary">
            <label className="library-search-field">
              <span>番号或标题</span>
              <span className="library-search-control">
                <Search aria-hidden="true" />
                <input
                  type="search"
                  value={draft.q}
                  placeholder="搜索番号或标题"
                  autoComplete="off"
                  spellCheck={false}
                  maxLength={120}
                  onChange={(event) => updateDraft('q', event.target.value)}
                />
              </span>
            </label>
            <label className="library-filter-field">
              <span>文件</span>
              <select value={draft.presence} onChange={(event) => updateDraft('presence', event.target.value)}>
                {presenceOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
            </label>
            <label className="library-filter-field">
              <span>完整度</span>
              <select value={draft.completeness} onChange={(event) => updateDraft('completeness', event.target.value)}>
                {completenessOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
            </label>
            <label className="library-filter-field">
              <span>异常</span>
              <select value={draft.anomaly} onChange={(event) => updateDraft('anomaly', event.target.value)}>
                {anomalyOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
            </label>
            <Button type="submit" variant="primary" disabled={library.isFetching}>
              <Filter aria-hidden="true" />
              筛选
            </Button>
          </div>

          <details className="library-filter-more" open={advancedCount > 0 || undefined}>
            <summary>
              <Filter aria-hidden="true" />
              <span>更多筛选</span>
              {advancedCount ? <small>{advancedCount} 项</small> : null}
              <ChevronDown aria-hidden="true" />
            </summary>
            <div className="library-filter-advanced">
              <label className="library-filter-field">
                <span>演员</span>
                <input value={draft.actor} maxLength={80} onChange={(event) => updateDraft('actor', event.target.value)} />
              </label>
              <label className="library-filter-field">
                <span>片商</span>
                <input value={draft.maker} maxLength={80} onChange={(event) => updateDraft('maker', event.target.value)} />
              </label>
              <label className="library-filter-field">
                <span>标签</span>
                <input value={draft.tag} maxLength={80} onChange={(event) => updateDraft('tag', event.target.value)} />
              </label>
              <label className="library-filter-field">
                <span>系列</span>
                <input value={draft.series} maxLength={80} onChange={(event) => updateDraft('series', event.target.value)} />
              </label>
              <label className="library-filter-field">
                <span>识别来源</span>
                <select value={draft.source} onChange={(event) => updateDraft('source', event.target.value)}>
                  <option value="">全部来源</option>
                  <option value="nfo">NFO</option>
                  <option value="path">文件路径</option>
                </select>
              </label>
              <label className="library-filter-field">
                <span>画质</span>
                <select value={draft.quality} onChange={(event) => updateDraft('quality', event.target.value)}>
                  {qualityOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
                </select>
              </label>
            </div>
          </details>

          {hasFilters ? (
            <div className="library-active-filters" role="status">
              <span>已应用 {activeFilterCount(appliedDraft)} 项筛选</span>
              <Button type="button" size="small" variant="ghost" onClick={clearFilters}>
                <X aria-hidden="true" />
                清除筛选
              </Button>
            </div>
          ) : null}
        </form>

        <div className="section-toolbar library-toolbar">
          <div>
            <StatusBadge tone={indexUnknown ? 'warning' : library.data?.ok ? 'success' : library.isLoading ? 'info' : 'neutral'}>
              <Library aria-hidden="true" />
              {indexUnknown ? '索引待确认' : '本地索引'}
            </StatusBadge>
            <span>{library.data?.ok ? `找到 ${count} 部作品` : library.isLoading ? '正在读取媒体库' : '媒体库状态不可用'}</span>
          </div>
          {library.isFetching && library.data ? <span className="polling-label">正在刷新</span> : null}
        </div>

        {library.data?.last_error_code === 'root_changed' ? (
          <InlineNotice tone="danger" role="alert">
            <strong>媒体库挂载身份已变化</strong>
            <span>索引已停止更新，确认当前挂载确实是目标媒体库后再接受并重建。</span>
            <Button
              type="button"
              size="small"
              onClick={() => {
                if (window.confirm('确认当前挂载是新的目标媒体库，并放弃旧索引后完整重建？')) {
                  rebuild.mutate(true)
                }
              }}
              disabled={rebuild.isPending}
            >
              <RefreshCw className={rebuild.isPending ? 'spin' : ''} aria-hidden="true" />
              接受新挂载并重建
            </Button>
          </InlineNotice>
        ) : indexUnknown ? (
          <InlineNotice tone="warning" role="status">
            媒体存储暂时无法确认，当前显示上次成功索引；文件状态统一标记为待确认。
          </InlineNotice>
        ) : null}
        {rebuild.isError ? (
          <InlineNotice tone="danger" role="alert">
            索引重建失败，请确认媒体目录可用且页面状态仍是最新版本后重试。
          </InlineNotice>
        ) : null}
        {library.isError && library.data ? (
          <InlineNotice tone="warning" role="status">
            <div>
              <span>刷新失败，正在显示上次读取的媒体库。</span>
              <Button type="button" size="small" variant="ghost" onClick={() => void library.refetch()} disabled={library.isFetching}>
                <RefreshCw className={library.isFetching ? 'spin' : ''} aria-hidden="true" />
                重试
              </Button>
            </div>
          </InlineNotice>
        ) : null}
        {library.isLoading ? <SkeletonRows count={7} /> : null}
        {library.isError && !library.data ? (
          <EmptyState
            role="alert"
            title="无法加载媒体库"
            description={libraryErrorMessage(library.error)}
            action={(
              <Button type="button" onClick={() => void library.refetch()} disabled={library.isFetching}>
                <RefreshCw className={library.isFetching ? 'spin' : ''} aria-hidden="true" />
                {library.isFetching ? '重试中' : '重新加载'}
              </Button>
            )}
          />
        ) : null}
        {unavailable ? (
          <EmptyState
            role="alert"
            title="媒体库索引暂不可用"
            description="请确认媒体目录和索引数据库可用后重试。"
            action={<Button type="button" onClick={() => void library.refetch()}>重新检测</Button>}
          />
        ) : null}
        {library.data?.ok && !library.data.items.length ? (
          <EmptyState
            role="status"
            title={hasFilters ? '没有匹配的媒体' : '媒体库索引中还没有作品'}
            description={hasFilters ? '调整筛选条件后重试。' : '完成媒体整理或索引重建后，作品会显示在这里。'}
            action={hasFilters ? (
              <Button type="button" variant="ghost" onClick={clearFilters}>
                <X aria-hidden="true" />
                清除筛选
              </Button>
            ) : undefined}
          />
        ) : null}

        {library.data?.ok && library.data.items.length ? (
          <div className="library-table" role="table" aria-label="媒体库作品列表">
            <div className="library-table-head" role="row">
              <span role="columnheader">作品</span>
              <span role="columnheader">资料</span>
              <span role="columnheader">完整度</span>
              <span role="columnheader">文件</span>
              <span role="columnheader">操作</span>
            </div>
            {library.data.items.map((entry) => (
              <LibraryRow entry={entry} returnPath={returnPath} key={entry.entry_id} />
            ))}
          </div>
        ) : null}

        {library.data?.ok && count > 0 ? (
          <nav className="history-pager" aria-label="媒体库分页">
            <IconButton
              label="上一页"
              size="small"
              onClick={() => goToPage(Math.max(1, page - 1))}
              disabled={page <= 1 || library.isFetching}
            >
              <ChevronLeft aria-hidden="true" />
            </IconButton>
            <span>第 {page} / {pageCount} 页，共 {count} 部</span>
            <IconButton
              label="下一页"
              size="small"
              onClick={() => goToPage(page + 1)}
              disabled={!library.data.has_more || library.isFetching}
            >
              <ChevronRight aria-hidden="true" />
            </IconButton>
          </nav>
        ) : null}
      </section>
    </div>
  )
}

function LibraryRow({ entry, returnPath }: { entry: MediaLibraryEntry; returnPath: string }) {
  const metadataUrl = metadataTarget(entry, returnPath)
  const workLabel = entry.code || '未识别番号'
  const incomplete = [entry.nfo_status, entry.portrait_status, entry.landscape_status]
    .some((status) => status !== 'present')
  return (
    <div className={`library-row library-presence-${entry.presence}`} role="row">
      <div className="library-work-cell" role="cell">
        <div className="library-work-heading">
          {entry.code ? <strong>{entry.code}</strong> : <span className="library-unidentified"><CircleHelp aria-hidden="true" />未识别番号</span>}
          {entry.variant ? <StatusBadge>{webDownloadVariantLabel(entry.variant)}</StatusBadge> : null}
          <StatusBadge tone={presenceTone(entry.presence)}>{presenceLabel(entry.presence)}</StatusBadge>
        </div>
        <span className="library-title" title={entry.title || undefined}>{entry.title || '本地媒体'}</span>
        <div className="library-anomalies">
          {entry.duplicate_count > 1 ? <StatusBadge tone="warning"><AlertTriangle aria-hidden="true" />{entry.duplicate_count} 个副本</StatusBadge> : null}
          {incomplete ? <StatusBadge tone="warning">元数据不完整</StatusBadge> : null}
          {entry.media_paths.length > 1 ? <StatusBadge>{entry.media_paths.length} 段</StatusBadge> : null}
        </div>
      </div>
      <div className="library-details-cell" role="cell">
        <strong title={entry.actors.join('、') || undefined}>{listLabel(entry.actors, '演员未知')}</strong>
        <span title={[...entry.makers, ...entry.publishers].join('、') || undefined}>
          {listLabel([...entry.makers, ...entry.publishers], '片商未知')}
          {entry.series.length ? ` · ${entry.series[0]}` : ''}
        </span>
        <span>{sourceLabel(entry.source)} · {qualityLabel(entry.quality_height)}{entry.release_date ? ` · ${entry.release_date}` : ''}</span>
        {entry.tags.length ? <span className="library-tags" title={entry.tags.join('、')}>{entry.tags.slice(0, 3).join('、')}</span> : null}
      </div>
      <div className="library-assets-cell" role="cell">
        <StatusBadge tone={assetTone(entry.nfo_status)}>
          {entry.nfo_status === 'present' ? <Check aria-hidden="true" /> : null}
          {assetLabel('NFO', entry.nfo_status)}
        </StatusBadge>
        <StatusBadge tone={assetTone(entry.portrait_status)}>{assetLabel('竖图', entry.portrait_status)}</StatusBadge>
        <StatusBadge tone={assetTone(entry.landscape_status)}>{assetLabel('横图', entry.landscape_status)}</StatusBadge>
      </div>
      <div className="library-file-cell" role="cell">
        <code title={entry.primary_media_path}>{entry.primary_media_path}</code>
        <span>
          <FolderOpen aria-hidden="true" />
          {entry.presence === 'missing'
            ? '文件记录已保留'
            : entry.presence === 'unknown'
              ? '等待存储恢复'
              : `${entry.media_paths.length} 个视频文件`}
        </span>
      </div>
      <div className="library-row-actions" role="cell">
        {metadataUrl ? (
          <Link
            to={metadataUrl}
            state={{ libraryReturnPath: returnPath }}
            className="library-metadata-link"
            aria-label={`补全 ${workLabel} 元数据`}
            title={`补全 ${workLabel} 元数据`}
            data-focus-return-key={`library:${entry.entry_id}`}
          >
            <FileImage aria-hidden="true" />
          </Link>
        ) : <span aria-hidden="true">-</span>}
      </div>
    </div>
  )
}
