import '../styles/library.css'
import { useMutation, useInfiniteQuery } from '@tanstack/react-query'
import {
  ChevronDown,
  Filter,
  Library,
  RefreshCw,
  Search,
  X,
} from 'lucide-react'
import { type FormEvent, useEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useSearchParams } from 'react-router-dom'

import { Button, EmptyState, IconButton, InlineNotice, PageHeader, SkeletonRows, StatusBadge } from '../components/ui'
import { ApiError, api } from '../lib/api'
import { webDownloadVariantLabel } from '../lib/webDownloads'
import type {
  MediaLibraryListParams,
  MediaLibraryPresence,
  MediaLibraryEntry,
} from '../types'
import { t } from '../lib/i18n'
import { LibraryDetail, LibraryPoster } from '../components/LibraryDetail'
import { LibraryFolder, folderLabel } from '../components/LibraryFolder'
import { BackLink } from '../components/BackLink'
import { useSelection, SelectionTools, SelectionCheck } from '../components/SelectionTools'

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
  { value: '', label: t('全部文件状态') },
  { value: 'present', label: t('文件存在') },
  { value: 'missing', label: t('文件已删除') },
  { value: 'unknown', label: t('存储待确认') },
]

const completenessOptions = [
  { value: '', label: t('全部完整度') },
  { value: 'complete', label: t('元数据完整') },
  { value: 'incomplete', label: t('元数据不完整') },
]

const anomalyOptions = [
  { value: '', label: t('全部异常') },
  { value: 'duplicate', label: t('重复番号') },
  { value: 'unidentified', label: t('无法识别番号') },
  { value: 'missing', label: t('文件已删除') },
  { value: 'nfo', label: t('缺少或无效 NFO') },
  { value: 'portrait', label: t('缺少竖版海报') },
  { value: 'landscape', label: t('缺少横版海报') },
  { value: 'subtitle', label: t('缺少字幕') },
]

const qualityOptions = [
  { value: '', label: t('全部画质') },
  { value: '2160', label: t('2160p 及以上') },
  { value: '1080', label: '1080p' },
  { value: '720', label: '720p' },
  { value: 'sd', label: t('低于 720p') },
]

function filtersFromParams(params: URLSearchParams): FilterDraft {
  return Object.fromEntries(
    FILTER_KEYS.map((key) => [key, (params.get(key) || '').trim()]),
  ) as FilterDraft
}

function appliedFilters(params: URLSearchParams): MediaLibraryListParams {
  const filters = filtersFromParams(params)
  const request: MediaLibraryListParams = {
    limit: LIBRARY_PAGE_SIZE,
    offset: 0,
    grouped: !params.get('group'),
    group: params.get('group') || undefined,
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
    return t('媒体库索引暂不可用，请确认媒体目录已挂载后重试。')
  }
  return t('无法读取媒体库，请稍后重试。')
}

export function LibraryPage() {
  const [params] = useSearchParams()
  return params.get('entry') ? <LibraryDetail entryId={params.get('entry')!} /> : <LibraryBrowser />
}

function LibraryBrowser() {
  const location = useLocation()
  const [searchParams, setSearchParams] = useSearchParams()
  const routeKey = searchParams.toString()
  const group = searchParams.get('group') || ''
  const selection = useSelection(routeKey)
  const sentinel = useRef<HTMLDivElement>(null)
  const applied = useMemo(() => appliedFilters(searchParams), [routeKey])
  const appliedDraft = useMemo(() => filtersFromParams(searchParams), [routeKey])
  const [draft, setDraft] = useState<FilterDraft>(appliedDraft)

  useEffect(() => {
    setDraft(appliedDraft)
  }, [appliedDraft])

  const pages = useInfiniteQuery({
    queryKey: ['media-library', routeKey],
    initialPageParam: 0,
    queryFn: ({ pageParam }) => api.mediaLibrary({ ...applied, offset: pageParam }),
    getNextPageParam: last => last.has_more ? last.offset + last.items.length : undefined,
    retry: false,
    staleTime: 15_000,
  })
  const items = Array.from(new Map(pages.data?.pages.flatMap(page => page.items).map(entry => [entry.entry_id, entry]) ?? []).values())
  const library = { ...pages, data: pages.data ? { ...pages.data.pages[0], items } : undefined }
  useEffect(() => {
    const node = sentinel.current
    if (!node || !pages.hasNextPage || pages.isFetching || pages.isError) return
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) void pages.fetchNextPage()
    }, { rootMargin: '600px' })
    observer.observe(node)
    return () => observer.disconnect()
  }, [pages.hasNextPage, pages.isFetching, pages.isError, pages.fetchNextPage])
  const rebuild = useMutation({
    mutationFn: (acceptRootChange: boolean) => api.rebuildMediaLibrary(
      library.data?.revision ?? 0,
      acceptRootChange,
    ),
    onSuccess: () => library.refetch(),
  })

  const count = library.data?.count ?? 0
  const hasFilters = activeFilterCount(appliedDraft) > 0
  const advancedCount = FILTER_KEYS
    .filter((key) => !['q', 'presence', 'completeness', 'anomaly'].includes(key))
    .filter((key) => draft[key]).length
  const returnPath = `${location.pathname}${location.search}`
  const [batchResult, setBatchResult] = useState('')
  const batch = useMutation({
    mutationFn: async (action: 'subtitles' | 'metadata' | 'export') => {
      const chosen: MediaLibraryEntry[] = []
      if (group) chosen.push(...items.filter(entry => selection.selected.has(entry.entry_id)))
      else for (const prefix of selection.selected) {
        let offset = 0
        for (;;) {
          const page = await api.mediaLibrary({ ...applied, grouped: false, group: prefix, limit: 200, offset })
          chosen.push(...page.items)
          if (chosen.length > 5000) throw new Error(t('每次最多处理 5000 部作品。'))
          if (!page.has_more) break
          offset += page.items.length
        }
      }
      if (action === 'export') {
        const url = URL.createObjectURL(new Blob([JSON.stringify(chosen, null, 2)], { type: 'application/json' }))
        const link = document.createElement('a'); link.href = url; link.download = 'jav-pilot-library.json'; link.click()
        window.setTimeout(() => URL.revokeObjectURL(url), 1000)
        return { done: chosen.length, failed: 0, skipped: 0 }
      }
      const eligible = chosen.filter(entry => entry.presence === 'present' && entry.code && (action !== 'subtitles' || entry.media_paths.length === 1))
      const queue = action === 'metadata' ? [...new Map(eligible.map(entry => [entry.code, entry])).values()] : eligible
      let done = 0, failed = 0
      const skipped = chosen.length - eligible.length
      await Promise.all(Array.from({ length: Math.min(3, queue.length) }, async () => {
        for (;;) {
          const entry = queue.shift()
          if (!entry) return
          try {
            if (action === 'subtitles') await api.subtitleAction({ action: 'fetch', entry_id: entry.entry_id })
            else await api.scanMediaMetadata(entry.code!)
            done++
          } catch { failed++ }
        }
      }))
      return { done, failed, skipped }
    },
    onSuccess: result => { setBatchResult(t('完成 {done} 项，失败 {failed} 项，跳过 {skipped} 项。', result)); if (!result.failed) selection.finish() },
    onError: (error: Error) => setBatchResult(error.message),
  })

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

  const unavailable = library.data && !library.data.ok
  const indexUnknown = library.data?.index_state === 'unknown'

  return (
    <div className="page library-page">
      {group ? <BackLink fallback={location.state?.libraryReturnPath || '/library'} /> : null}
      <PageHeader
        title={group ? folderLabel(group) : t('媒体库')}
        description={group ? t('选择作品，查看详情并播放。') : t('按番号前缀浏览本地作品，进入目录选择影片。')}
        actions={(
          <IconButton
            label={t('重建媒体库索引')}
            onClick={() => rebuild.mutate(false)}
            disabled={library.isFetching || rebuild.isPending}
          >
            <RefreshCw className={library.isFetching || rebuild.isPending ? 'spin' : ''} aria-hidden="true" />
          </IconButton>
        )}
      />
      <SelectionTools selection={selection} ids={items.map(entry => group ? entry.entry_id : entry.group_key!)} busy={batch.isPending}>
        <Button size="small" disabled={!selection.selected.size || batch.isPending} onClick={() => batch.mutate('subtitles')}>{t('获取字幕')}</Button>
        <Button size="small" disabled={!selection.selected.size || batch.isPending} onClick={() => batch.mutate('metadata')}>{t('补全所选番号')}</Button>
        <Button size="small" disabled={!selection.selected.size || batch.isPending} onClick={() => batch.mutate('export')}>{t('导出所选')}</Button>
      </SelectionTools>
      {selection.managing ? <p className="selection-note">{group ? t('仅处理所选作品；元数据补全按番号处理全部版本。') : t('选择目录后处理其中全部作品，包含尚未滚动加载的作品。')}</p> : null}
      {batchResult ? <p className="selection-note" role="status">{batchResult}</p> : null}

      <section className="library-workspace" aria-label={t('媒体库作品')}>
        {!group ? <form className="library-filter-form" role="search" onSubmit={submitFilters}>
          <div className="library-filter-primary">
            <label className="library-search-field">
              <span>{t('番号或标题')}</span>
              <span className="library-search-control">
                <Search aria-hidden="true" />
                <input
                  type="search"
                  value={draft.q}
                  placeholder={t('搜索番号或标题')}
                  autoComplete="off"
                  spellCheck={false}
                  maxLength={120}
                  onChange={(event) => updateDraft('q', event.target.value)}
                />
              </span>
            </label>
            <label className="library-filter-field">
              <span>{t('文件')}</span>
              <select value={draft.presence} onChange={(event) => updateDraft('presence', event.target.value)}>
                {presenceOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
            </label>
            <label className="library-filter-field">
              <span>{t('完整度')}</span>
              <select value={draft.completeness} onChange={(event) => updateDraft('completeness', event.target.value)}>
                {completenessOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
            </label>
            <label className="library-filter-field">
              <span>{t('异常')}</span>
              <select value={draft.anomaly} onChange={(event) => updateDraft('anomaly', event.target.value)}>
                {anomalyOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
              </select>
            </label>
            <Button type="submit" variant="primary" disabled={library.isFetching}>
              <Filter aria-hidden="true" />
              {t('筛选')}
            </Button>
          </div>

          <details className="library-filter-more" open={advancedCount > 0 || undefined}>
            <summary>
              <Filter aria-hidden="true" />
              <span>{t('更多筛选')}</span>
              {advancedCount ? <small>{t('{advancedCount} 项', { advancedCount })}</small> : null}
              <ChevronDown aria-hidden="true" />
            </summary>
            <div className="library-filter-advanced">
              <label className="library-filter-field">
                <span>{t('演员')}</span>
                <input value={draft.actor} maxLength={80} onChange={(event) => updateDraft('actor', event.target.value)} />
              </label>
              <label className="library-filter-field">
                <span>{t('片商')}</span>
                <input value={draft.maker} maxLength={80} onChange={(event) => updateDraft('maker', event.target.value)} />
              </label>
              <label className="library-filter-field">
                <span>{t('标签')}</span>
                <input value={draft.tag} maxLength={80} onChange={(event) => updateDraft('tag', event.target.value)} />
              </label>
              <label className="library-filter-field">
                <span>{t('系列')}</span>
                <input value={draft.series} maxLength={80} onChange={(event) => updateDraft('series', event.target.value)} />
              </label>
              <label className="library-filter-field">
                <span>{t('识别来源')}</span>
                <select value={draft.source} onChange={(event) => updateDraft('source', event.target.value)}>
                  <option value="">{t('全部来源')}</option>
                  <option value="nfo">NFO</option>
                  <option value="path">{t('文件路径')}</option>
                </select>
              </label>
              <label className="library-filter-field">
                <span>{t('画质')}</span>
                <select value={draft.quality} onChange={(event) => updateDraft('quality', event.target.value)}>
                  {qualityOptions.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
                </select>
              </label>
            </div>
          </details>

          {hasFilters ? (
            <div className="library-active-filters" role="status">
              <span>{t('已应用 {value} 项筛选', { value: activeFilterCount(appliedDraft) })}</span>
              <Button type="button" size="small" variant="ghost" onClick={clearFilters}>
                <X aria-hidden="true" />
                {t('清除筛选')}
              </Button>
            </div>
          ) : null}
        </form> : null}

        <div className="section-toolbar library-toolbar">
          <div>
            <StatusBadge tone={indexUnknown ? 'warning' : library.data?.ok ? 'success' : library.isLoading ? 'info' : 'neutral'}>
              <Library aria-hidden="true" />
              {indexUnknown ? t('索引待确认') : t('本地索引')}
            </StatusBadge>
            <span>{library.data?.ok ? group ? t('找到 {count} 部作品', { count }) : t('共 {count} 个目录', { count }) : library.isLoading ? t('正在读取媒体库') : t('媒体库状态不可用')}</span>
          </div>
          {library.isFetching && library.data ? <span className="polling-label">{t('正在刷新')}</span> : null}
        </div>

        {library.data?.last_error_code === 'root_changed' ? (
          <InlineNotice tone="danger" role="alert">
            <strong>{t('媒体库挂载身份已变化')}</strong>
            <span>{t('索引已停止更新，确认当前挂载确实是目标媒体库后再接受并重建。')}</span>
            <Button
              type="button"
              size="small"
              onClick={() => {
                if (window.confirm(t('确认当前挂载是新的目标媒体库，并放弃旧索引后完整重建？'))) {
                  rebuild.mutate(true)
                }
              }}
              disabled={rebuild.isPending}
            >
              <RefreshCw className={rebuild.isPending ? 'spin' : ''} aria-hidden="true" />
              {t('接受新挂载并重建')}
            </Button>
          </InlineNotice>
        ) : indexUnknown ? (
          <InlineNotice tone="warning" role="status">
            {t('媒体存储暂时无法确认，当前显示上次成功索引；文件状态统一标记为待确认。')}
          </InlineNotice>
        ) : null}
        {rebuild.isError ? (
          <InlineNotice tone="danger" role="alert">
            {t('索引重建失败，请确认媒体目录可用且页面状态仍是最新版本后重试。')}
          </InlineNotice>
        ) : null}
        {library.isError && library.data ? (
          <InlineNotice tone="warning" role="status">
            <div>
              <span>{t('刷新失败，正在显示上次读取的媒体库。')}</span>
              <Button type="button" size="small" variant="ghost" onClick={() => void library.refetch()} disabled={library.isFetching}>
                <RefreshCw className={library.isFetching ? 'spin' : ''} aria-hidden="true" />
                {t('重试')}
              </Button>
            </div>
          </InlineNotice>
        ) : null}
        {library.isLoading ? <SkeletonRows count={7} /> : null}
        {library.isError && !library.data ? (
          <EmptyState
            role="alert"
            title={t('无法加载媒体库')}
            description={libraryErrorMessage(library.error)}
            action={(
              <Button type="button" onClick={() => void library.refetch()} disabled={library.isFetching}>
                <RefreshCw className={library.isFetching ? 'spin' : ''} aria-hidden="true" />
                {library.isFetching ? t('重试中') : t('重新加载')}
              </Button>
            )}
          />
        ) : null}
        {unavailable ? (
          <EmptyState
            role="alert"
            title={t('媒体库索引暂不可用')}
            description={t('请确认媒体目录和索引数据库可用后重试。')}
            action={<Button type="button" onClick={() => void library.refetch()}>{t('重新检测')}</Button>}
          />
        ) : null}
        {library.data?.ok && !library.data.items.length ? (
          <EmptyState
            role="status"
            title={hasFilters ? t('没有匹配的媒体') : t('媒体库索引中还没有作品')}
            description={hasFilters ? t('调整筛选条件后重试。') : t('完成媒体整理或索引重建后，作品会显示在这里。')}
            action={hasFilters ? (
              <Button type="button" variant="ghost" onClick={clearFilters}>
                <X aria-hidden="true" />
                {t('清除筛选')}
              </Button>
            ) : undefined}
          />
        ) : null}

        {library.data?.ok && library.data.items.length ? (
          <div className="library-poster-grid" aria-label={t('媒体库作品列表')}>
            {items.map(entry => {
              if (!group) return <div className={selection.managing ? 'selection-poster' : undefined} key={entry.group_key}><SelectionCheck selection={selection} id={entry.group_key!} name={folderLabel(entry.group_key!)} disabled={batch.isPending} /><LibraryFolder prefix={entry.group_key!} count={entry.group_count!} revision={entry.revision} returnPath={returnPath} /></div>
              const target = `/library?${new URLSearchParams({ entry: entry.entry_id })}`
              return <article className={`library-poster-card${selection.managing ? ' selection-poster' : ''}`} key={entry.entry_id}>
                <SelectionCheck selection={selection} id={entry.entry_id} name={entry.code || entry.title || t('本地媒体')} disabled={batch.isPending} />
                <Link to={target} state={{ libraryReturnPath: returnPath }} className="library-poster-link" aria-label={entry.code || entry.title || t('本地媒体')}>
                  <LibraryPoster entry={entry} />
                </Link>
                <div className="library-poster-meta"><div className="watch-inline-actions"><strong>{entry.code || t('未识别番号')}</strong>
                  {entry.variant ? <StatusBadge>{webDownloadVariantLabel(entry.variant)}</StatusBadge> : null}
                </div><h2><Link to={target} state={{ libraryReturnPath: returnPath }}>{entry.title || t('本地媒体')}</Link></h2>
                <p>{[entry.quality_height ? `${entry.quality_height}p` : '', ...entry.actors.slice(0, 2)].filter(Boolean).join(' · ')}</p>
                {entry.presence !== 'present' ? <StatusBadge tone="warning">{entry.presence === 'missing' ? t('文件已删除') : t('存储待确认')}</StatusBadge> : null}
                </div>
              </article>
            })}
          </div>
        ) : null}

        <div ref={sentinel} className="library-load-status" role="status">{pages.isFetchingNextPage ? t('正在加载') : null}</div>
        {pages.isFetchNextPageError ? <Button onClick={() => void pages.fetchNextPage()}>{t('重试')}</Button> : null}
      </section>
    </div>
  )
}
