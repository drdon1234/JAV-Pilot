import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ExternalLink, FileText, Languages, Layers3, RefreshCw, Search, Star, X } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { Button, EmptyState, IconButton, InlineNotice, PageHeader, ProgressBar, SkeletonRows, StatusBadge, Toggle } from '../components/ui'
import { useToast } from '../components/ToastProvider'
import { api, ApiError, coverImageUrl } from '../lib/api'
import { clearPersistedDetailPrefetchBatchId, detailPrefetchIsActive, detailPrefetchStatusLabel, persistDetailPrefetchBatchId, storedDetailPrefetchBatchId } from '../lib/detailPrefetchSession'
import { externalHttpUrl } from '../lib/format'
import { loadSearchPreferences } from '../lib/searchPreferences'
import { SEARCH_CAPABILITIES, SEARCH_PARSER_PROFILES } from '../lib/sources'
import { useTranslationPreferences, useTranslations } from '../lib/translation'
import { useAiTranslations } from '../lib/aiTranslation'
import { AiTranslateButton, AiTranslationLine } from '../components/AiTranslation'
import type { DetailPrefetchItemStatus, RankingBoard, RankingBoardsPayload, RankingGroup, RankingItem, RankingOption } from '../types'
import { QuickWebDownload } from './QuickWebDownload'
import { StableImage } from './WorkUi'
import { t } from '../lib/i18n'
import { serverText } from '../lib/serverTexts'

import '../styles/rankings.css'

const GROUPS: Array<{ value: RankingGroup; label: string }> = [
  { value: 'works', label: t('作品榜') },
  { value: 'actors', label: t('女优榜') },
  { value: 'genres', label: t('分类榜') },
]
const STORAGE_KEY = 'jav-pilot:rankings-view'

const PREFETCH_STATUS: Record<DetailPrefetchItemStatus, { label: string; tone: 'neutral' | 'success' | 'warning' | 'info' }> = {
  queued: { label: t('等待解析'), tone: 'neutral' },
  running: { label: t('解析中'), tone: 'info' },
  completed: { label: t('详情已就绪'), tone: 'success' },
  failed: { label: t('解析失败'), tone: 'warning' },
}

/** Detail page link for a ranked work; search results use the same work id, so parsed details are shared. */
function detailHref(item: RankingItem): string | null {
  if (!item.work_id || !item.code) return null
  return `/works/${encodeURIComponent(item.work_id)}?${new URLSearchParams({ code: item.code }).toString()}`
}

interface StoredView {
  board?: string
  groupBoards?: Partial<Record<RankingGroup, string>>
  views?: Record<string, { period?: string; category?: string }>
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value)
}

function optionalString(value: unknown): string | undefined {
  return typeof value === 'string' ? value : undefined
}

function loadStoredView(): StoredView {
  let parsed: unknown
  try {
    parsed = JSON.parse(window.localStorage.getItem(STORAGE_KEY) ?? 'null')
  } catch {
    return {}
  }
  if (!isRecord(parsed)) return {}
  const groupBoards: StoredView['groupBoards'] = {}
  if (isRecord(parsed.groupBoards)) {
    for (const group of ['works', 'actors', 'genres'] as const) {
      const id = optionalString(parsed.groupBoards[group])
      if (id) groupBoards[group] = id
    }
  }
  const views: NonNullable<StoredView['views']> = {}
  if (isRecord(parsed.views)) {
    for (const [id, view] of Object.entries(parsed.views)) {
      if (isRecord(view)) views[id] = { period: optionalString(view.period), category: optionalString(view.category) }
    }
  }
  return { board: optionalString(parsed.board), groupBoards, views }
}

function saveStoredView(view: StoredView): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(view))
  } catch {
    // Remembering the last board is a convenience; private windows may block storage.
  }
}

function OptionGroup({ label, options, value, onChange }: {
  label: string
  options: readonly RankingOption[]
  value: string
  onChange: (value: string) => void
}) {
  return (
    <div className="segmented-control rankings-segmented" role="group" aria-label={label}>
      {options.map((option) => (
        <button type="button" className={value === option.value ? 'active' : ''} aria-pressed={value === option.value} onClick={() => onChange(option.value)} key={option.value}>{option.label}</button>
      ))}
    </div>
  )
}

/** 排行榜: work, actress and genre rankings from JavDB, FANZA, FC2 and other sources, with search and download shortcuts. */
/** Board labels and notes arrive in Chinese; show them in the interface language. */
function localizeBoards(payload: RankingBoardsPayload): RankingBoardsPayload {
  const option = (item: RankingOption): RankingOption => ({ ...item, label: serverText(item.label) })
  return {
    ...payload,
    boards: payload.boards.map((board) => ({
      ...board,
      label: serverText(board.label),
      source: serverText(board.source),
      note: serverText(board.note),
      periods: board.periods.map(option),
      categories: board.categories.map(option),
    })),
  }
}

export function RankingsPage() {
  const [stored, setStored] = useState<StoredView>(loadStoredView)
  const latestStored = useRef(stored)
  latestStored.current = stored
  const [refreshTarget, setRefreshTarget] = useState<{ key: string; nonce: number } | null>(null)
  const toast = useToast()
  const queryClient = useQueryClient()
  // One background detail parser serves the whole app; the search page shows the same batch.
  const [prefetchBatchId, setPrefetchBatchId] = useState(storedDetailPrefetchBatchId)
  const prefetchCreateLock = useRef(false)
  const prefetch = useQuery({
    queryKey: ['detail-prefetch-batch', prefetchBatchId],
    queryFn: () => api.detailPrefetchBatch(prefetchBatchId),
    enabled: Boolean(prefetchBatchId),
    refetchInterval: (query) => detailPrefetchIsActive(query.state.data) ? 1_000 : false,
    refetchIntervalInBackground: false,
    retry: false,
  })
  const prefetchBatch = prefetch.data
  const prefetchActive = detailPrefetchIsActive(prefetchBatch)
  const createPrefetch = useMutation({
    mutationFn: (codes: string[]) => api.createDetailPrefetchBatchForCodes(codes),
    onSuccess: (batch) => {
      queryClient.setQueryData(['detail-prefetch-batch', batch.batch_id], batch)
      setPrefetchBatchId(batch.batch_id)
      persistDetailPrefetchBatchId(batch.batch_id)
      toast.push(t('已将 {total} 部作品交给后台解析详情', { total: batch.total }), 'success')
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
    onSettled: () => {
      prefetchCreateLock.current = false
    },
  })
  const cancelPrefetch = useMutation({
    mutationFn: (batchId: string) => api.cancelDetailPrefetchBatch(batchId),
    onSuccess: (batch) => {
      queryClient.setQueryData(['detail-prefetch-batch', batch.batch_id], batch)
      toast.push(t('已停止后台详情解析'), 'success')
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const translation = useTranslationPreferences(settings.data?.settings.workflow_defaults?.translation)
  const boardsQuery = useQuery({ queryKey: ['ranking-boards'], queryFn: async () => localizeBoards(await api.rankingBoards()), staleTime: Infinity, retry: 1 })
  const boards = boardsQuery.data?.boards ?? []
  const board: RankingBoard | undefined = boards.find((item) => item.id === stored.board) ?? boards[0]
  const view = board ? stored.views?.[board.id] : undefined
  const period = board?.periods.find((option) => option.value === view?.period)?.value ?? board?.periods[0]?.value ?? ''
  const category = !board
    ? ''
    : board.dynamic_categories
      ? view?.category ?? ''
      : board.categories.find((option) => option.value === view?.category)?.value ?? board.categories[0]?.value ?? ''
  const viewKey = `${board?.id ?? ''}|${period}|${category}`
  const refresh = refreshTarget?.key === viewKey
  const ranking = useQuery({
    queryKey: ['rankings', board?.id, period, category, refresh ? refreshTarget?.nonce : 0],
    queryFn: () => api.rankings({ board: board?.id ?? '', period, category }, refresh),
    enabled: Boolean(board && period),
    staleTime: 10 * 60_000,
    retry: false,
  })
  const webService = useQuery({
    queryKey: ['web-download-service'],
    queryFn: () => api.webDownloads({ limit: 1 }),
    retry: false,
    staleTime: 15_000,
  })
  const webConfigured = webService.data?.configured === true && webService.data.enabled !== false
  const items = ranking.data?.items ?? []
  const worksBoard = board?.item_kind === 'work'
  const translatable = worksBoard ? items.map((item) => item.title) : []
  const titles = useTranslations(translatable, translation.preferences.enabled && worksBoard)
  const aiTitles = useAiTranslations(translatable)
  const sites = settings.data?.settings.sites ?? []
  const searchSources = sites
    .filter((site) => site.enabled && SEARCH_CAPABILITIES.some((capability) => site.capabilities.includes(capability)) && SEARCH_PARSER_PROFILES.has(site.parser_profile))
    .map((site) => site.id)
  const errorCode = ranking.error instanceof ApiError ? ranking.error.code : null
  const staleCategory = errorCode === 'invalid' && Boolean(board?.dynamic_categories && category)
  const categories = board?.dynamic_categories ? ranking.data?.categories ?? [] : board?.categories ?? []
  const shownCategory = board?.dynamic_categories ? ranking.data?.category ?? category : category

  useEffect(() => {
    if (prefetchBatch && !detailPrefetchIsActive(prefetchBatch)) clearPersistedDetailPrefetchBatchId(prefetchBatch.batch_id)
  }, [prefetchBatch])
  useEffect(() => {
    // A remembered batch the server no longer has would be re-requested on every visit.
    if (prefetch.error instanceof ApiError && prefetch.error.status === 404) {
      clearPersistedDetailPrefetchBatchId(prefetchBatchId)
      setPrefetchBatchId('')
    }
  }, [prefetch.error, prefetchBatchId])

  useEffect(() => {
    // A remembered genre can disappear from the source's list; fall back to its default.
    if (staleCategory && board) {
      updateStored((current) => ({
        ...current,
        views: { ...current.views, [board.id]: { ...current.views?.[board.id], category: '' } },
      }))
    }
  }, [staleCategory, board])

  // Storage is written outside the state updater, which StrictMode may run twice.
  function updateStored(change: (current: StoredView) => StoredView) {
    const next = change(latestStored.current)
    latestStored.current = next
    setStored(next)
    saveStoredView(next)
  }

  function selectBoard(next: RankingBoard) {
    updateStored((current) => ({
      ...current,
      board: next.id,
      groupBoards: { ...current.groupBoards, [next.group]: next.id },
    }))
  }

  function selectGroup(group: RankingGroup) {
    const remembered = boards.find((item) => item.id === stored.groupBoards?.[group] && item.group === group)
    const next = remembered ?? boards.find((item) => item.group === group)
    if (next) selectBoard(next)
  }

  function updateView(patch: { period?: string; category?: string }) {
    if (!board) return
    updateStored((current) => ({
      ...current,
      board: board.id,
      views: { ...current.views, [board.id]: { ...current.views?.[board.id], ...patch } },
    }))
  }

  function searchHref(query: string, kind: 'code' | 'actor'): string {
    const params = new URLSearchParams({
      q: query,
      source: searchSources.join(','),
      result_limit: '20',
      magnets: loadSearchPreferences().fetchMagnets ? '1' : '0',
      sort: 'relevance',
      match: kind === 'code' ? 'exact' : 'auto',
      kind,
      site_mode: 'all',
      page: '1',
      page_size: '20',
    })
    return `/results?${params.toString()}`
  }

  const detailCodes = [...new Set(items.filter((item) => item.work_id && item.code).map((item) => item.code as string))]
  const prefetchItems = new Map((prefetchBatch?.items ?? []).map((item) => [item.work_id, item.status]))
  const prefetchDone = prefetchBatch ? prefetchBatch.completed + prefetchBatch.failed : 0

  function startPrefetch() {
    if (!detailCodes.length || prefetchActive || prefetchCreateLock.current) return
    prefetchCreateLock.current = true
    createPrefetch.mutate(detailCodes)
  }

  const groupBoards = board ? boards.filter((item) => item.group === board.group) : []
  const knownError = errorCode === 'login_required' || errorCode === 'source_disabled' || errorCode === 'region_restricted'

  return (
    <div className="page rankings-page">
      <PageHeader
        title={t('排行榜')}
        description={t('汇总 JavDB、FANZA、FC2、MGStage 与无码厂商官网的作品、女优和分类榜单')}
        actions={(
          <IconButton
            label={t('刷新榜单')}
            onClick={() => setRefreshTarget((current) => ({ key: viewKey, nonce: (current?.nonce ?? 0) + 1 }))}
            disabled={!board || ranking.isFetching}
          >
            <RefreshCw className={ranking.isFetching ? 'spin' : ''} aria-hidden="true" />
          </IconButton>
        )}
      />
      {boardsQuery.isError ? (
        <EmptyState
          title={t('暂时无法加载榜单列表')}
          description={t('请检查服务是否正常运行后重试。')}
          action={<Button onClick={() => void boardsQuery.refetch()}>{t('重新加载')}</Button>}
        />
      ) : null}
      {board ? (
        <div className="rankings-toolbar">
          <div className="rankings-selectors">
            <OptionGroup label={t('榜单类型')} options={GROUPS} value={board.group} onChange={(value) => selectGroup(value as RankingGroup)} />
            <OptionGroup
              label={t('榜单来源')}
              options={groupBoards.map((item) => ({ value: item.id, label: item.label }))}
              value={board.id}
              onChange={(value) => {
                const next = boards.find((item) => item.id === value)
                if (next) selectBoard(next)
              }}
            />
            {board.periods.length > 1 ? (
              <OptionGroup label={board.group === 'genres' ? t('排序方式') : t('时间范围')} options={board.periods} value={period} onChange={(value) => updateView({ period: value })} />
            ) : null}
            {board.dynamic_categories ? (
              <label className="rankings-category-select">
                <span className="sr-only">{t('分类')}</span>
                <select
                  value={shownCategory}
                  disabled={!categories.length}
                  onChange={(event) => updateView({ category: event.target.value })}
                >
                  {categories.length ? null : <option value={shownCategory}>{t('正在加载分类')}</option>}
                  {categories.map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
                </select>
              </label>
            ) : categories.length > 1 ? (
              <OptionGroup label={t('榜单类别')} options={categories} value={category} onChange={(value) => updateView({ category: value })} />
            ) : null}
          </div>
          {worksBoard ? (
            <div className="rankings-translation">
              <Toggle label={t('翻译标题')} checked={translation.preferences.enabled} onChange={(event) => translation.update({ enabled: event.target.checked })} />
              {titles.loading ? <span className="rankings-translating"><Languages className="spin" aria-hidden="true" />{t('正在翻译')}</span> : null}
              <AiTranslateButton state={aiTitles} />
            </div>
          ) : null}
          {board.note && !knownError ? <p className="rankings-note">{board.note}</p> : null}
        </div>
      ) : null}
      {errorCode === 'login_required' ? (
        <InlineNotice tone="info" role="status">
          {t('JavDB 只公开有码榜单和演员榜；无码、欧美和 FC2 作品榜需要登录后的 JavDB 会话（可在部署环境中配置 JavDB Cookie）。')}
        </InlineNotice>
      ) : null}
      {errorCode === 'source_disabled' ? (
        <InlineNotice tone="warning" role="status">
          {t('该榜单来自 JavDB，请先在')} <Link to="/sites">{t('站点')}</Link> {t('页面启用 JavDB。')}
        </InlineNotice>
      ) : null}
      {errorCode === 'region_restricted' ? (
        <InlineNotice tone="warning" role="status">
          {t('MGStage 只允许日本 IP 访问，请将 JAV_PILOT_PROXY 设置为日本节点的代理后刷新。')}
        </InlineNotice>
      ) : null}
      {ranking.isError && !knownError && !staleCategory ? (
        <EmptyState
          title={t('暂时无法获取榜单')}
          description={t('{value} 可能正在进行访问验证或暂时不可用，请稍后刷新。', { value: board?.source ?? t('榜单来源') })}
          action={<Button onClick={() => setRefreshTarget((current) => ({ key: viewKey, nonce: (current?.nonce ?? 0) + 1 }))}>{t('重新获取')}</Button>}
        />
      ) : null}
      {worksBoard && detailCodes.length ? (
        <div className="rankings-prefetch" role="group" aria-label={t('批量详情操作')}>
          <div className="rankings-prefetch-actions">
            <Button
              type="button"
              size="small"
              variant="secondary"
              disabled={prefetchActive || createPrefetch.isPending || (Boolean(prefetchBatchId) && prefetch.isPending)}
              onClick={startPrefetch}
            >
              <Layers3 aria-hidden="true" />
              {t('解析本页详情（{count}）', { count: detailCodes.length })}
            </Button>
            {prefetchActive && prefetchBatch ? (
              <Button
                type="button"
                size="small"
                variant="secondary"
                disabled={cancelPrefetch.isPending}
                onClick={() => cancelPrefetch.mutate(prefetchBatch.batch_id)}
              >
                <X aria-hidden="true" />
                {cancelPrefetch.isPending ? t('正在取消') : t('取消解析')}
              </Button>
            ) : null}
            <span className="rankings-prefetch-hint">{t('在后台逐部解析详情，完成后点“详情”即可直接打开')}</span>
          </div>
          {prefetchBatch ? (
            <div className="rankings-prefetch-progress" aria-live="polite">
              <ProgressBar value={prefetchBatch.total ? prefetchDone / prefetchBatch.total : 0} label={t('详情解析进度')} />
              <span>
                {t('{value}：{completed} / {total}', { value: detailPrefetchStatusLabel(prefetchBatch, cancelPrefetch.isPending), completed: prefetchBatch.completed, total: prefetchBatch.total })}
                {prefetchBatch.failed ? t('，失败 {failed}', { failed: prefetchBatch.failed }) : ''}
              </span>
            </div>
          ) : null}
        </div>
      ) : null}
      {boardsQuery.isLoading || ranking.isLoading ? <SkeletonRows count={6} /> : null}
      {board && items.length ? (
        <ol className="rankings-list" aria-label={worksBoard ? t('榜单作品') : t('榜单人物')}>
          {items.map((item) => {
            const cover = item.cover ? coverImageUrl(item.source_id, item.cover, sites) : ''
            const detailUrl = externalHttpUrl(item.detail_url)
            const openLabel = t('在 {source} 打开 {value}', { source: board.source, value: item.code ?? item.title })
            if (!worksBoard) {
              return (
                <li className="ranking-row ranking-person" key={`${item.rank}-${item.title}`}>
                  <span className="ranking-rank"><span className="sr-only">{t('第 {rank} 名', { rank: item.rank })}</span><span aria-hidden="true">{item.rank}</span></span>
                  <div className="ranking-avatar">
                    <StableImage src={cover} alt={t('{title} 头像', { title: item.title })} referrerPolicy="no-referrer" />
                  </div>
                  <div className="ranking-main">
                    <strong>{item.title}</strong>
                    {item.subtitle ? <span className="ranking-meta">{item.subtitle}</span> : null}
                  </div>
                  <div className="ranking-actions">
                    {board.item_kind === 'actor' ? (
                      <Link className="button button-secondary button-small" to={searchHref(item.title, 'actor')}>
                        <Search aria-hidden="true" />
                        {t('搜索')}
                      </Link>
                    ) : null}
                    {detailUrl ? (
                      <a className="button button-ghost button-small" href={detailUrl} target="_blank" rel="noopener noreferrer" aria-label={openLabel}>
                        <ExternalLink aria-hidden="true" />
                      </a>
                    ) : null}
                  </div>
                </li>
              )
            }
            const translated = translation.preferences.enabled ? titles.translate(item.title) : null
            const detailLink = detailHref(item)
            const prefetchStatus = item.work_id ? prefetchItems.get(item.work_id) : undefined
            return (
              <li className="ranking-row" key={`${item.rank}-${item.code ?? item.title}`}>
                <span className="ranking-rank"><span className="sr-only">{t('第 {rank} 名', { rank: item.rank })}</span><span aria-hidden="true">{item.rank}</span></span>
                <div className="ranking-cover">
                  <StableImage src={cover} alt={t('{value} 封面', { value: item.code ?? item.title })} referrerPolicy="no-referrer" />
                </div>
                <div className="ranking-main">
                  <span className="ranking-code-line">
                    <strong>{item.code ?? t('未知番号')}</strong>
                    {prefetchStatus ? <StatusBadge tone={PREFETCH_STATUS[prefetchStatus].tone}>{PREFETCH_STATUS[prefetchStatus].label}</StatusBadge> : null}
                  </span>
                  <p>{translated || item.title}</p>
                  {translated && translation.preferences.showOriginal ? <small>{item.title}</small> : null}
                  <AiTranslationLine text={aiTitles.translate(item.title)} />
                  <span className="ranking-meta">
                    {item.release_date || t('日期未知')}
                    {item.subtitle ? <span className="ranking-subtitle">{item.subtitle}</span> : null}
                    {item.rating !== null ? <><Star aria-hidden="true" /> {item.rating}{item.votes !== null ? t('（{votes} 人）', { votes: item.votes }) : ''}</> : null}
                  </span>
                </div>
                <div className="ranking-actions">
                  {detailLink ? (
                    <Link className="button button-secondary button-small" to={detailLink} state={{ returnTo: '/rankings' }} aria-label={t('查看 {code} 详情', { code: item.code })}>
                      <FileText aria-hidden="true" />
                      {t('详情')}
                    </Link>
                  ) : null}
                  {item.code ? (
                    <Link className="button button-secondary button-small" to={searchHref(item.code, 'code')}>
                      <Search aria-hidden="true" />
                      {t('搜索')}
                    </Link>
                  ) : null}
                  {item.code && webConfigured ? <QuickWebDownload code={item.code} /> : null}
                  {detailUrl ? (
                    <a className="button button-ghost button-small" href={detailUrl} target="_blank" rel="noopener noreferrer" aria-label={openLabel}>
                      <ExternalLink aria-hidden="true" />
                    </a>
                  ) : null}
                </div>
              </li>
            )
          })}
        </ol>
      ) : null}
    </div>
  )
}
