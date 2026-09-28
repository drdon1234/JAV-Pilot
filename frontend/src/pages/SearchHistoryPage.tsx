import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Clapperboard, Database, RefreshCw, Search, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { useNavigate } from 'react-router-dom'

import { useToast } from '../components/ToastProvider'
import { Button, EmptyState, IconButton, InlineNotice, PageHeader, SkeletonRows, StatusBadge } from '../components/ui'
import { api } from '../lib/api'
import { formatDateTime } from '../lib/format'
import { serviceErrorMessage } from '../lib/presentation'
import { loadSearchPreferences, SEARCH_KIND_OPTIONS } from '../lib/searchPreferences'
import { searchSessionParams, type SearchSessionRequest, useSearchSessions } from '../lib/searchSessions'
import { SEARCH_CAPABILITIES, SEARCH_PARSER_PROFILES } from '../lib/sources'
import type { SearchHistoryItem, SearchKind, SearchMatch, SearchSort } from '../types'
import { t } from '../lib/i18n'

import '../styles/searchHistory.css'

function text(value: unknown): string {
  return typeof value === 'string' ? value : ''
}

function stringRecord(value: unknown): Record<string, string> {
  if (!value || typeof value !== 'object') return {}
  return Object.fromEntries(Object.entries(value as Record<string, unknown>).filter((entry): entry is [string, string] => typeof entry[1] === 'string'))
}

/**
 * 搜索记录: every site search and Web search is saved automatically. One card
 * type serves both; opening a card runs the same search again and lands on
 * the matching results page.
 */
export function SearchHistoryPage() {
  const toast = useToast()
  const navigate = useNavigate()
  const queryClient = useQueryClient()
  const sessions = useSearchSessions()
  const [kind, setKind] = useState<'all' | 'metadata' | 'resource'>('all')
  const [pendingId, setPendingId] = useState('')
  const settings = useQuery({ queryKey: ['settings'], queryFn: api.settings })
  const history = useQuery({
    queryKey: ['search-history', kind],
    queryFn: () => api.searchHistory({ limit: 200, kind: kind === 'all' ? undefined : kind }),
  })
  const remove = useMutation({
    mutationFn: (id: string) => api.searchHistoryAction({ action: 'remove', id }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ['search-history'] }),
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const clear = useMutation({
    mutationFn: () => api.searchHistoryAction({ action: 'clear' }),
    onSuccess: (payload) => {
      toast.push(t('已清空 {value} 条搜索记录', { value: payload.cleared ?? 0 }), 'success')
      void queryClient.invalidateQueries({ queryKey: ['search-history'] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const [confirmClear, setConfirmClear] = useState(false)

  const sites = settings.data?.settings.sites ?? []
  const siteName = (id: string) => sites.find((site) => site.id === id)?.name ?? id
  const allSearchSites = sites
    .filter((site) => site.enabled && SEARCH_CAPABILITIES.some((capability) => site.capabilities.includes(capability)) && SEARCH_PARSER_PROFILES.has(site.parser_profile))
    .map((site) => site.id)

  async function rerun(item: SearchHistoryItem) {
    if (pendingId) return
    if (item.kind === 'metadata' && !settings.data) {
      toast.push(settings.isError ? t('无法读取站点配置，请刷新后重试') : t('站点配置仍在加载，请稍后再试'), 'error')
      return
    }
    setPendingId(item.id)
    try {
      if (item.kind === 'metadata') {
        const params = item.params
        const siteMode = params.site_mode === 'all' ? 'all' : 'custom'
        const storedSources = Array.isArray(params.sources) ? params.sources.filter((value): value is string => typeof value === 'string') : []
        const sources = siteMode === 'all' ? allSearchSites : storedSources.filter((id) => allSearchSites.includes(id))
        if (!sources.length) {
          toast.push(t('这条记录使用的站点已全部停用，请在搜索页重新选择站点'), 'error')
          return
        }
        const request: SearchSessionRequest = {
          query: text(params.query) || item.query,
          sources,
          resultLimit: typeof params.result_limit === 'number' ? params.result_limit : 100,
          fetchMagnets: params.fetch_magnets !== false,
          filters: stringRecord(params.filters),
          sort: (text(params.sort) || 'relevance') as SearchSort,
          match: (text(params.match) || 'auto') as SearchMatch,
          searchKind: (text(params.search_kind) || 'keyword') as SearchKind,
          semanticRefs: stringRecord(params.semantic_refs),
        }
        sessions.refreshSession(request)
        const routeParams = searchSessionParams(request)
        routeParams.set('site_mode', siteMode)
        routeParams.set('page', '1')
        routeParams.set('page_size', String(loadSearchPreferences().pageSize))
        navigate(`/results?${routeParams.toString()}`)
        return
      }
      const params = item.params
      const start = text(params.start) || (typeof params.start === 'number' ? String(params.start) : '')
      const end = text(params.end) || (typeof params.end === 'number' ? String(params.end) : '')
      const resultLimit = typeof params.result_limit === 'number' ? params.result_limit : 100
      const payload = await api.createResourceSearch({
        source_id: text(params.source_id) || 'all',
        query: text(params.query) || item.query,
        result_limit: resultLimit,
        exact_match: params.exact_match === true,
        ...(start && end ? { start, end, suffix_width: typeof params.suffix_width === 'number' ? params.suffix_width : Math.max(start.length, end.length) } : {}),
      })
      const routeParams = new URLSearchParams({
        workspace: 'resource',
        resource_id: payload.search.session_id,
        resource_query: text(params.query) || item.query,
        result_limit: String(resultLimit),
      })
      navigate(`/search?${routeParams.toString()}`)
    } catch (error) {
      toast.push(serviceErrorMessage(error, t('无法重新搜索，请稍后重试')), 'error')
    } finally {
      setPendingId('')
    }
  }

  function describe(item: SearchHistoryItem): string[] {
    const params = item.params
    if (item.kind === 'metadata') {
      const sources = Array.isArray(params.sources) ? params.sources.filter((value): value is string => typeof value === 'string') : []
      return [
        params.site_mode === 'all' ? t('全部站点') : sources.map(siteName).join(t('、')) || t('自选站点'),
        SEARCH_KIND_OPTIONS.find((option) => option.value === params.search_kind)?.label ?? t('全部内容'),
        params.match === 'exact' ? t('精确匹配') : '',
        params.fetch_magnets === false ? t('不解析磁链') : t('解析磁链'),
        typeof params.result_limit === 'number' ? t('上限 {result_limit}', { result_limit: params.result_limit }) : '',
      ].filter(Boolean)
    }
    const start = params.start ?? null
    const end = params.end ?? null
    return [
      text(params.source_id) === 'all' || !params.source_id ? t('全部 Web 站点') : siteName(text(params.source_id)),
      params.exact_match === true ? t('精确匹配') : '',
      start !== null && end !== null ? t('范围 {start}–{end}', { start: String(start), end: String(end) }) : '',
      typeof params.result_limit === 'number' ? t('上限 {result_limit}', { result_limit: params.result_limit }) : '',
    ].filter(Boolean)
  }

  const items = history.data?.items ?? []
  return (
    <div className="page search-history-page">
      <PageHeader
        title={t('搜索记录')}
        description={t('自动保存的站点搜索与 Web 搜索，点击即可按原条件重新搜索')}
        actions={(
          <>
            <IconButton label={t('刷新搜索记录')} onClick={() => void history.refetch()} disabled={history.isFetching}>
              <RefreshCw className={history.isFetching ? 'spin' : ''} aria-hidden="true" />
            </IconButton>
            {confirmClear ? (
              <>
                <Button variant="danger" size="small" onClick={() => { setConfirmClear(false); clear.mutate() }} disabled={clear.isPending}>{t('确认清空')}</Button>
                <Button variant="ghost" size="small" onClick={() => setConfirmClear(false)}>{t('取消')}</Button>
              </>
            ) : (
              <Button variant="ghost" size="small" onClick={() => setConfirmClear(true)} disabled={!items.length || clear.isPending}>
                <Trash2 aria-hidden="true" />
                {t('清空记录')}
              </Button>
            )}
          </>
        )}
      />
      <div className="segmented-control search-history-filter" role="group" aria-label={t('记录类型')}>
        {([['all', t('全部')], ['metadata', t('站点搜索')], ['resource', t('Web 搜索')]] as const).map(([value, label]) => (
          <button type="button" className={kind === value ? 'active' : ''} aria-pressed={kind === value} onClick={() => setKind(value)} key={value}>{label}</button>
        ))}
      </div>
      {history.isError ? (
        <InlineNotice tone="warning" role="alert">{serviceErrorMessage(history.error, t('暂时无法读取搜索记录'))}</InlineNotice>
      ) : null}
      {history.isLoading ? <SkeletonRows count={5} /> : null}
      {history.data && !items.length ? (
        <EmptyState title={t('还没有搜索记录')} description={t('在搜索页进行的站点搜索和 Web 搜索会自动保存在这里')} />
      ) : null}
      {items.length ? (
        <ul className="search-history-list" aria-label={t('搜索记录')}>
          {items.map((item) => (
            <li className="search-history-card" key={item.id}>
              <button type="button" className="search-history-open" onClick={() => void rerun(item)} disabled={Boolean(pendingId)} aria-busy={pendingId === item.id}>
                <span className="search-history-icon" aria-hidden="true">
                  {item.kind === 'metadata' ? <Database /> : <Clapperboard />}
                </span>
                <span className="search-history-main">
                  <strong>{item.query}</strong>
                  <span>{describe(item).join(' · ')}</span>
                </span>
                <span className="search-history-meta">
                  <StatusBadge tone={item.kind === 'metadata' ? 'info' : 'neutral'}>{item.kind === 'metadata' ? t('站点搜索') : t('Web 搜索')}</StatusBadge>
                  <span>{formatDateTime(item.used_at)}{item.use_count > 1 ? t(' · {use_count} 次', { use_count: item.use_count }) : ''}</span>
                </span>
                <span className="search-history-go" aria-hidden="true">
                  {pendingId === item.id ? <RefreshCw className="spin" /> : <Search />}
                </span>
              </button>
              <IconButton label={t('删除记录 {query}', { query: item.query })} size="small" className="danger-icon" onClick={() => remove.mutate(item.id)} disabled={remove.isPending}>
                <Trash2 aria-hidden="true" />
              </IconButton>
            </li>
          ))}
        </ul>
      ) : null}
      {history.data ? (
        <p className="search-history-note">{t('最多保留 {limit} 条，可在“默认参数”页调整。', { limit: history.data.limit })}</p>
      ) : null}
    </div>
  )
}
