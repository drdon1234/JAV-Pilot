import { watchCoverUrl } from '../lib/watching'
import { useInfiniteQuery, useQuery } from '@tanstack/react-query'
import { ArrowRight, Bookmark, ExternalLink, Play, RefreshCw, Search, Settings } from 'lucide-react'
import { BackLink } from '../components/BackLink'
import { useEffect, useRef, useState } from 'react'
import { Link, useNavigate, useSearchParams } from 'react-router-dom'

import { BookmarkButton } from '../components/WatchActions'
import { WatchPlayer } from '../components/WatchPlayer'
import { Button, EmptyState, InlineNotice, PageHeader, SkeletonRows, StatusBadge } from '../components/ui'
import { t } from '../lib/i18n'
import { editionLabel, watching, watchHref, type WatchEntry, type WatchSite } from '../lib/watching'
import { StableImage } from './WorkUi'

function EntryCard({ entry, sourceName, query }: { entry: WatchEntry; sourceName: string; query: string }) {
  return <article className="watch-card">
    <Link className="watch-card-cover" to={watchHref(entry, false, query)} tabIndex={-1} aria-hidden="true">
      <StableImage src={watchCoverUrl(entry.source, entry.cover)} alt="" loading="lazy" referrerPolicy="no-referrer" />
    </Link>
    <div className="watch-card-body">
      <div className="watch-inline-actions"><strong>{entry.code || sourceName}</strong>{entry.editions.map(edition =>
        <StatusBadge key={edition} tone={edition === '原片' ? 'neutral' : 'info'}>{editionLabel(edition)}</StatusBadge>)}</div>
      <h3><Link to={watchHref(entry, false, query)}>{entry.title}</Link></h3>
      <span className="watch-help">{sourceName}</span>
    </div>
    <div className="watch-card-actions">
      <Link className="button button-primary button-small" to={watchHref(entry, true, query)}><Play aria-hidden="true" />{t('应用内播放')}</Link>
      <BookmarkButton entry={entry} kind="work" />
    </div>
  </article>
}

function SiteSummary({ site, query }: { site: WatchSite; query: string }) {
  const result = useQuery({
    queryKey: ['watch-catalog', site.id, query, 1],
    queryFn: ({ signal }) => watching.catalog(site.id, query, 1, signal),
    retry: false, staleTime: 60_000,
  })
  const entries = result.data?.items ?? []
  const editions = Array.from(new Set(entries.flatMap(entry => entry.editions)))
  const href = `/watch?${new URLSearchParams({ source: site.id, ...(query ? { q: query } : {}) })}`
  return <article className="watch-site-summary">
    <div className="watch-site-name"><h2><Link to={href}>{site.name}</Link></h2><span className="watch-help">{new URL(site.base_url).hostname}</span></div>
    <div className="watch-summary-state" aria-live="polite">
      {result.isPending ? <span className="watch-help" role="status">{t('正在检索并完成后台验证…')}</span>
        : result.isError ? <span className="watch-summary-error">{result.error.message}</span>
          : <><StatusBadge tone={entries.length ? 'success' : 'neutral'}>{entries.length ? t('{count} 个结果', { count: entries.length }) : t('暂无结果')}</StatusBadge>
            {editions.map(edition => <StatusBadge key={edition}>{editionLabel(edition)}</StatusBadge>)}</>}
    </div>
    <div className="watch-summary-actions">
      {result.isError ? <Button type="button" variant="ghost" size="small" onClick={() => void result.refetch()} disabled={result.isFetching}>
        <RefreshCw aria-hidden="true" />{t('重试此站点')}</Button> : null}
      <Link className={`button button-small ${entries.length ? 'button-primary' : 'button-secondary'}`} to={href}>
        {query ? t('查看结果') : t('进入站点')}<ArrowRight aria-hidden="true" />
      </Link>
    </div>
  </article>
}

function SiteResults({ site, query }: { site: WatchSite; query: string }) {
  const result = useInfiniteQuery({
    queryKey: ['watch-catalog-scroll', site.id, query],
    initialPageParam: 1,
    queryFn: ({ signal, pageParam }) => watching.catalog(site.id, query, pageParam, signal),
    getNextPageParam: last => last.has_more && last.page < 100 ? last.page + 1 : undefined,
    retry: false, staleTime: 60_000,
  })
  const sentinel = useRef<HTMLDivElement>(null)
  const entries = Array.from(new Map(result.data?.pages.flatMap(page => page.items).map(entry => [entry.url, entry]) ?? []).values())
  useEffect(() => {
    const node = sentinel.current
    if (!node || !result.hasNextPage || result.isFetching || result.isError) return
    const observer = new IntersectionObserver(([entry]) => {
      if (entry.isIntersecting) void result.fetchNextPage()
    }, { rootMargin: '500px' })
    observer.observe(node)
    return () => observer.disconnect()
  }, [result.hasNextPage, result.isFetching, result.isError, result.fetchNextPage])
  return <section className="watch-site-results" aria-label={site.name}>
    <header className="watch-section-heading"><h2>{site.name}</h2><div className="watch-inline-actions">
      {result.isFetching ? <span role="status" className="watch-help">{t('正在检索并完成后台验证…')}</span>
        : result.data ? <span className="watch-help">{t('{count} 个结果', { count: entries.length })}</span> : null}
      <Button type="button" size="small" variant="ghost" disabled={result.isFetching} onClick={() => void result.refetch()}><RefreshCw aria-hidden="true" />{t('刷新')}</Button>
    </div></header>
    {result.isPending ? <SkeletonRows count={2} /> : entries.length ? <div className="watch-grid">
      {entries.map(entry => <EntryCard key={entry.url} entry={entry} sourceName={site.name} query={query} />)}
    </div> : <p className="watch-empty-line">{query ? t('此站点未找到对应内容，可尝试其他番号或关键词。') : t('此站点暂时没有可展示的作品。')}</p>}
    <div ref={sentinel} className="watch-pagination" role="status">{result.isFetchingNextPage ? t('正在加载') : null}</div>
    {result.isError ? <InlineNotice tone="warning" role="alert">{result.error.message} <Button type="button" size="small" onClick={() => void (result.isFetchNextPageError ? result.fetchNextPage() : result.refetch())}>{t('重试此站点')}</Button></InlineNotice> : null}
  </section>
}

export function SelectedWork({ source, url, play, onPlay }: {
  source: string; url: string; play: boolean; onPlay: () => void
}) {
  const detail = useQuery({ queryKey: ['watch-detail', source, url], queryFn: ({ signal }) => watching.detail(source, url, signal), retry: false, staleTime: 60_000, refetchOnWindowFocus: false })
  const related = useQuery({ queryKey: ['watch-recommendations', source, url], queryFn: ({ signal }) => watching.recommendations(source, url, signal), enabled: source === 'missav' && !!detail.data && !detail.data.recommendations?.length, retry: false, staleTime: 300_000, refetchOnWindowFocus: false })
  if (detail.isPending) return <SkeletonRows count={3} />
  if (detail.isError) return <EmptyState title={t('无法加载站点作品')} description={detail.error.message} action={<div className="watch-inline-actions">
    <Button type="button" onClick={() => void detail.refetch()}>{t('重新加载')}</Button>
  </div>} />
  const entry = detail.data
  return <>
    <div className="watch-work-top">
      <a href={entry.url} className="button button-ghost button-small" target="_blank" rel="noopener noreferrer">{t('查看来源')}<ExternalLink aria-hidden="true" /></a></div>
    {play ? <WatchPlayer key={`${source}:${url}`} entry={entry} /> : null}
    <section className={`watch-work-info${play ? ' watch-playing-info' : ''}`}>
      <div className="watch-work-cover"><StableImage src={watchCoverUrl(entry.source, entry.cover)} alt={t('作品封面')} referrerPolicy="no-referrer" /></div>
      <div><div className="watch-inline-actions"><strong>{entry.code || source}</strong>{entry.editions.map(edition => <StatusBadge key={edition}>{editionLabel(edition)}</StatusBadge>)}</div>
        <h2>{entry.title}</h2>{entry.description ? <p className="watch-work-description">{entry.description}</p> : null}
        <div className="watch-inline-actions">
          {!play ? <Button type="button" variant="primary" onClick={onPlay}><Play aria-hidden="true" />{t('开始播放')}</Button> : null}
          <BookmarkButton entry={entry} kind="work" />
          {!play ? <BookmarkButton entry={entry} kind="playback" /> : null}
        </div>
      </div>
    </section>
    {!play ? <p className="watch-help">{t('播放将在应用内打开。可分别收藏站点作品页和直接播放入口。')}</p> : null}
    {entry.recommendations?.length || related.data?.items.length ? <section className="watch-site-results"><h2>{t('推荐作品')}</h2><div className="watch-grid">{(entry.recommendations?.length ? entry.recommendations : related.data?.items ?? []).map(item => <EntryCard key={item.url} entry={item} sourceName={source} query="" />)}</div></section> : related.isFetching ? <SkeletonRows count={2} /> : related.isError ? <InlineNotice tone="warning">{t('推荐作品')} · {related.error.message} <Button onClick={() => void related.refetch()}>{t('重试')}</Button></InlineNotice> : null}
  </>
}

export function WatchPage() {
  const navigate = useNavigate()
  const [params, setParams] = useSearchParams()
  const query = params.get('q')?.trim() || ''
  const selectedSource = params.get('source') || ''
  const selectedUrl = params.get('url') || ''
  const [draft, setDraft] = useState(query)
  const sites = useQuery({ queryKey: ['watch-sites'], queryFn: watching.sites })
  const enabled = sites.data?.sites.filter(site => site.enabled) ?? []
  useEffect(() => { setDraft(query) }, [query, selectedSource])
  function update(values: Record<string, string>) { setParams(values) }
  const active = selectedSource ? enabled.filter(site => site.id === selectedSource) : enabled
  return <div className="page watch-page">
    {selectedUrl || selectedSource ? <BackLink fallback="/watch" /> : null}
    <PageHeader title={t('在线观看')} description={t('浏览已启用站点，按版本选择作品，在应用内播放。')}
      actions={<><Link className="button button-secondary button-small" to="/favorites"><Bookmark aria-hidden="true" />{t('收藏')}</Link>
        <Link className="button button-secondary button-small" to="/sites"><Settings aria-hidden="true" />{t('管理站点')}</Link></>} />
    {selectedUrl ? <SelectedWork source={selectedSource} url={selectedUrl} play={params.get('play') === '1'}
      onPlay={() => navigate(watchHref({ source: selectedSource, url: selectedUrl }, true, query))} /> : <>
      <form className="watch-search" onSubmit={event => { event.preventDefault(); update({ ...(selectedSource ? { source: selectedSource } : {}), ...(draft.trim() ? { q: draft.trim() } : {}) }) }}>
        <label className="sr-only" htmlFor="watch-query">{t('搜索番号、作品名或演员')}</label>
        <Search aria-hidden="true" /><input id="watch-query" value={draft} maxLength={120} onChange={event => setDraft(event.target.value)} placeholder={t('搜索番号、作品名或演员')} />
        <Button type="submit" variant="primary">{t('检索站点')}</Button>
      </form>
      <nav className="watch-source-tabs" aria-label={t('在线观看站点')}>
        <Button type="button" variant={!selectedSource ? 'primary' : 'ghost'} aria-pressed={!selectedSource} onClick={() => update(query ? { q: query } : {})}>{t('全部启用站点')}</Button>
        {sites.data?.sites.map(site => <Button key={site.id} type="button" disabled={!site.enabled} aria-pressed={selectedSource === site.id} variant={selectedSource === site.id ? 'primary' : 'ghost'}
          onClick={() => update({ source: site.id, ...(query ? { q: query } : {}) })}>{site.name}{!site.enabled ? ` · ${t('未启用')}` : ''}</Button>)}
      </nav>
      {query ? <p className="watch-help watch-search-note">{t('原片、字幕版和无码版分别显示，请核对后播放。')}</p> : null}
      {sites.isPending ? <SkeletonRows /> : sites.isError ? <EmptyState title={t('无法加载在线观看站点')} description={sites.error.message}
        action={<Button type="button" onClick={() => void sites.refetch()}>{t('重试')}</Button>} />
        : !active.length ? <EmptyState title={t('没有启用的在线观看站点')} description={t('启用 JableTV、SupJav 或 MissAV 后，即可浏览和检索作品。')}
          action={<Link className="button button-primary" to="/sites">{t('前往站点设置')}</Link>} />
        : selectedSource || active.length === 1 ? active.map(site => <SiteResults key={`${site.id}:${query}`} site={site} query={query} />)
          : <div className="watch-site-list">{active.map(site => <SiteSummary key={`${site.id}:${query}`} site={site} query={query} />)}</div>}
    </>}
  </div>
}
