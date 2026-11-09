import { api } from '../lib/api'
import { createWebDownloadIntentKey } from '../lib/webDownloads'
import { useToast } from '../components/ToastProvider'
import { SelectionCheck, SelectionTools, useSelection } from '../components/SelectionTools'
import { PlaybackPreview } from '../components/PlaybackPreview'
import { usePlaybackActivity } from '../lib/playbackActivity'
import { watchCoverUrl } from '../lib/watching'
import { useInfiniteQuery, useMutation, useQuery } from '@tanstack/react-query'
import { ArrowLeft, ArrowRight, Bookmark, Download, ExternalLink, Play, RefreshCw, Search, Settings } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'
import { Link, useLocation, useNavigate, useSearchParams } from 'react-router-dom'

import { BookmarkButton } from '../components/WatchActions'
import { WatchPlayer } from '../components/WatchPlayer'
import { Button, EmptyState, InlineNotice, PageHeader, SkeletonRows, StatusBadge } from '../components/ui'
import { t } from '../lib/i18n'
import { editionLabel, watching, watchHref, type WatchEntry, type WatchSite } from '../lib/watching'
import { StableImage } from './WorkUi'

function EntryCard({ entry, sourceName, query }: { entry: WatchEntry; sourceName: string; query: string }) {
  const location = useLocation()
  const params = new URLSearchParams(location.search)
  params.set('source', entry.source); params.set('url', entry.url); params.delete('play')
  const detailHref = `${location.pathname === '/search' ? '/search' : '/watch'}?${params}`
  return <article className="watch-card">
    <Link className="watch-card-cover" to={detailHref} tabIndex={-1} aria-hidden="true">
      <StableImage src={watchCoverUrl(entry.source, entry.cover)} alt="" loading="lazy" referrerPolicy="no-referrer" />
    </Link>
    <div className="watch-card-body">
      <div className="watch-inline-actions"><strong>{entry.code || sourceName}</strong>{entry.editions.map(edition =>
        <StatusBadge key={edition} tone={edition === '原片' ? 'neutral' : 'info'}>{editionLabel(edition)}</StatusBadge>)}</div>
      <h3><Link to={detailHref}>{entry.title}</Link></h3>
      <span className="watch-help">{sourceName}</span>
    </div>
    <div className="watch-card-actions">
      <Link className="button button-primary button-small" to={watchHref(entry, true, query)}><Play aria-hidden="true" />{t('应用内播放')}</Link>
      <DownloadWork entry={entry} />
      <BookmarkButton entry={entry} kind="work" />
    </div>
  </article>
}

function downloadVariant(entry: WatchEntry) {
  if (entry.editions.includes('中文字幕')) return 'chinese_subtitle'
  if (entry.editions.some(value => value === '无码' || value === '无码流出')) return 'uncensored_leak'
  return 'original'
}

function DownloadWork({ entry }: { entry: WatchEntry }) {
  const toast = useToast()
  const download = useMutation({
    mutationFn: () => api.addWebDownload(entry.code, createWebDownloadIntentKey(), { provider: entry.source, variant: downloadVariant(entry) }),
    onSuccess: () => toast.push(t('已加入下载队列'), 'success'),
    onError: (error: Error) => toast.push(error.message, 'error'),
  })
  return <Button type="button" size="small" disabled={!entry.code || download.isPending} onClick={() => download.mutate()}><Download aria-hidden="true" />{download.isPending ? t('正在提交') : t('下载')}</Button>
}

function useSiteCatalog(site: WatchSite, query: string) {
  return useInfiniteQuery({
    queryKey: ['watch-catalog-scroll', site.id, query], initialPageParam: 1,
    queryFn: ({ signal, pageParam }) => watching.catalog(site.id, query, pageParam, signal),
    getNextPageParam: (last, pages) => {
      if (!last.has_more || last.page >= 9999) return undefined
      const previous = new Set(pages.slice(0, -1).flatMap(page => page.items.map(entry => entry.url)))
      return last.items.length && last.items.every(entry => previous.has(entry.url)) ? undefined : last.page + 1
    },
    retry: false, staleTime: 60_000,
  })
}

function AutoLoad({ enabled, loading, onLoad }: { enabled: boolean; loading: boolean; onLoad: () => void }) {
  const end = useRef<HTMLDivElement>(null)
  useEffect(() => {
    if (!enabled || loading || !end.current) return
    const observer = new IntersectionObserver(entries => { if (entries.some(entry => entry.isIntersecting)) { observer.disconnect(); onLoad() } }, { rootMargin: '200px 0px' })
    observer.observe(end.current)
    return () => observer.disconnect()
  }, [enabled, loading, onLoad])
  return <div ref={end} className="catalog-load-boundary" role="status">{loading ? t('正在加载') : null}</div>
}

function SiteShelf({ site, query, href }: { site: WatchSite; query: string; href: string }) {
  const result = useSiteCatalog(site, query)
  const entries = Array.from(new Map(result.data?.pages.flatMap(page => page.items).map(entry => [entry.url, entry]) ?? []).values())
  const rail = useRef<HTMLDivElement>(null)
  const [edges, setEdges] = useState({ start: true, end: true })
  useEffect(() => {
    const element = rail.current
    if (!element) return
    const update = () => setEdges({ start: Math.abs(element.scrollLeft) < 2, end: Math.abs(element.scrollLeft) + element.clientWidth >= element.scrollWidth - 2 })
    const observer = new ResizeObserver(update)
    observer.observe(element); element.addEventListener('scroll', update, { passive: true }); update()
    return () => { observer.disconnect(); element.removeEventListener('scroll', update) }
  }, [result.data])
  const scroll = (direction: number) => {
    const element = rail.current
    if (element) element.scrollBy({ left: direction * (getComputedStyle(element).direction === 'rtl' ? -1 : 1) * element.clientWidth * 0.85, behavior: window.matchMedia('(prefers-reduced-motion: reduce)').matches ? 'instant' : 'smooth' })
  }
  return <section className="web-site-shelf" aria-label={site.name}>
    <header className="watch-section-heading"><div><h2>{site.name}</h2><span className="watch-help">{new URL(site.base_url).hostname}</span></div><Link className="button button-ghost button-small" to={href}>{t('进入站点')}<ArrowRight aria-hidden="true" /></Link></header>
    {result.isPending ? <SkeletonRows count={1} /> : result.isError && !entries.length ? <InlineNotice tone="warning">{result.error.message} <Button type="button" size="small" onClick={() => void result.refetch()}>{t('重试此站点')}</Button></InlineNotice>
      : entries.length ? <div className="web-site-carousel"><button type="button" className="web-site-scroll web-site-scroll-start" aria-label={`${site.name} ${t('上一页')}`} disabled={edges.start} onClick={() => scroll(-1)}><ArrowLeft aria-hidden="true" /></button><div ref={rail} className="web-site-rail" tabIndex={0} aria-label={t('{name} 作品', { name: site.name })}>{entries.map(entry => <EntryCard key={entry.url} entry={entry} sourceName={site.name} query={query} />)}{result.hasNextPage ? <AutoLoad enabled={!result.isError} loading={result.isFetching} onLoad={() => void result.fetchNextPage()} /> : null}</div><button type="button" className="web-site-scroll web-site-scroll-end" aria-label={`${site.name} ${t('下一页')}`} disabled={edges.end} onClick={() => scroll(1)}><ArrowRight aria-hidden="true" /></button></div>
      : <p className="watch-empty-line">{t('此站点暂时没有可展示的作品。')}</p>}
    {result.isFetchNextPageError ? <InlineNotice tone="warning">{result.error.message} <Button type="button" onClick={() => void result.fetchNextPage()}>{t('重试此站点')}</Button></InlineNotice> : null}
  </section>
}

function SiteResults({ site, query }: { site: WatchSite; query: string }) {
  const [params, setParams] = useSearchParams()
  const keyword = params.get('filter') ?? '', edition = params.get('edition') ?? '', order = params.get('order') ?? ''
  const filter = (name: string, value: string) => setParams(previous => { const next = new URLSearchParams(previous); if (value) next.set(name, value); else next.delete(name); return next }, { replace: true })
  const selection = useSelection(`${site.id}:${query}`)
  const toast = useToast()
  const result = useSiteCatalog(site, query)
  const allEntries = Array.from(new Map(result.data?.pages.flatMap(page => page.items).map(entry => [entry.url, entry]) ?? []).values())
  const entries = allEntries.filter(entry => (!edition || entry.editions.includes(edition)) && (!keyword || `${entry.code} ${entry.title}`.toLocaleLowerCase().includes(keyword.toLocaleLowerCase())))
  if (order) entries.sort((a, b) => (order === 'desc' ? -1 : 1) * a.code.localeCompare(b.code, undefined, { numeric: true }))
  const bulk = useMutation({ mutationFn: async () => {
    let added = 0
    for (const entry of allEntries.filter(item => selection.selected.has(item.url))) {
      await api.addWebDownload(entry.code, createWebDownloadIntentKey(), { provider: site.id, variant: downloadVariant(entry) })
      selection.toggle(entry.url); added++
    }
    return added
  }, onSuccess: count => { selection.finish(); toast.push(t('已加入 {count} 项下载', { count }), 'success') }, onError: (error: Error) => toast.push(error.message, 'error') })
  return <section className="watch-site-results" aria-label={site.name}>
    <header className="watch-section-heading"><h2>{site.name}</h2><div className="watch-inline-actions"><span className="watch-help">{t('已加载 {count} 个作品', { count: allEntries.length })}</span><Button type="button" size="small" disabled={result.isFetching} onClick={() => void result.refetch()}><RefreshCw aria-hidden="true" />{t('刷新')}</Button></div></header>
    <fieldset className="web-site-filters"><legend>{t('筛选当前站点已加载作品')}</legend>
      <label>{t('关键词')}<input value={keyword} onChange={event => filter('filter', event.target.value)} placeholder={t('番号或标题')} /></label>
      <label>{t('版本')}<select aria-label={t('版本')} value={edition} onChange={event => filter('edition', event.target.value)}><option value="">{t('全部')}</option>{['原片', '中文字幕', '无码', '无码流出', '英文字幕'].map(value => <option key={value} value={value}>{editionLabel(value)}</option>)}</select></label>
      <label>{t('排序')}<select aria-label={t('排序')} value={order} onChange={event => filter('order', event.target.value)}><option value="">{t('站点默认')}</option><option value="asc">{t('番号升序')}</option><option value="desc">{t('番号降序')}</option></select></label>
    </fieldset>
    <SelectionTools selection={selection} ids={entries.filter(entry => entry.code).map(entry => entry.url)} busy={bulk.isPending}><Button type="button" size="small" disabled={!selection.selected.size || bulk.isPending} onClick={() => bulk.mutate()}><Download aria-hidden="true" />{t('下载所选')}</Button></SelectionTools>
    {result.isPending ? <SkeletonRows count={2} /> : entries.length ? <div className="watch-grid">{entries.map(entry => <div key={entry.url} className="selection-poster"><SelectionCheck selection={selection} id={entry.url} name={entry.title} disabled={!entry.code || bulk.isPending} /><EntryCard entry={entry} sourceName={site.name} query={query} /></div>)}</div> : <p className="watch-empty-line">{t('没有符合条件的作品，可调整筛选或继续加载。')}</p>}
    {result.isError ? <InlineNotice tone="warning">{result.error.message} <Button type="button" onClick={() => void (result.isFetchNextPageError ? result.fetchNextPage() : result.refetch())}>{t('重试此站点')}</Button></InlineNotice> : null}
    {result.hasNextPage ? <AutoLoad enabled={!result.isError} loading={result.isFetching} onLoad={() => void result.fetchNextPage()} /> : null}
  </section>
}

export function SelectedWork({ source, url, play, onPlay }: {
  source: string; url: string; play: boolean; onPlay: () => void
}) {
  const activity = usePlaybackActivity()
  const detail = useQuery({ queryKey: ['watch-detail', source, url], queryFn: ({ signal }) => watching.detail(source, url, signal), retry: false, staleTime: 60_000, refetchOnWindowFocus: false })
  const related = useQuery({ queryKey: ['watch-recommendations', source, url], queryFn: ({ signal }) => watching.recommendations(source, url, signal), enabled: source === 'missav' && !!detail.data && !detail.data.recommendations?.length, retry: false, staleTime: 300_000, refetchOnWindowFocus: false })
  if (detail.isPending) return play && activity.snapshot ? <PlaybackPreview /> : <SkeletonRows count={3} />
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
          <DownloadWork entry={entry} />
          <BookmarkButton entry={entry} kind="work" />
          {!play ? <BookmarkButton entry={entry} kind="playback" /> : null}
        </div>
      </div>
    </section>
    {!play ? <p className="watch-help">{t('播放将在应用内打开。可分别收藏站点作品页和直接播放入口。')}</p> : null}
    {entry.recommendations?.length || related.data?.items.length ? <section className="watch-site-results"><h2>{t('推荐作品')}</h2><div className="watch-grid">{(entry.recommendations?.length ? entry.recommendations : related.data?.items ?? []).map(item => <EntryCard key={item.url} entry={item} sourceName={source} query="" />)}</div></section> : related.isFetching ? <SkeletonRows count={2} /> : related.isError ? <InlineNotice tone="warning">{t('推荐作品')} · {related.error.message} <Button onClick={() => void related.refetch()}>{t('重试')}</Button></InlineNotice> : null}
  </>
}

export function WebCatalogWorkspace() {
  const navigate = useNavigate()
  const location = useLocation()
  const [params, setParams] = useSearchParams()
  const query = params.get('q')?.trim() || ''
  const selectedSource = params.get('source') || ''
  const selectedUrl = params.get('url') || ''
  const [draft, setDraft] = useState(query)
  const sites = useQuery({ queryKey: ['watch-sites'], queryFn: watching.sites })
  const enabled = sites.data?.sites.filter(site => site.enabled) ?? []
  useEffect(() => { setDraft(query) }, [query, selectedSource])
  const routeParams = (values: Record<string, string>) => new URLSearchParams({ ...(location.pathname === '/search' ? { workspace: 'resource' } : {}), ...values })
  const href = (source = '') => `${location.pathname}?${routeParams({ ...(source ? { source } : {}), ...(query ? { q: query } : {}) })}`
  const active = selectedSource ? enabled.filter(site => site.id === selectedSource) : enabled
  return <div className="web-catalog-workspace">
    {selectedUrl ? <><Link className="button button-ghost" to={href(selectedSource)}><ArrowLeft aria-hidden="true" />{t('返回站点')}</Link><SelectedWork source={selectedSource} url={selectedUrl} play={false} onPlay={() => navigate(watchHref({ source: selectedSource, url: selectedUrl }, true, query))} /></> : <>
      <div className="web-catalog-tools">{selectedSource ? <Link className="button button-ghost" to={href()}><ArrowLeft aria-hidden="true" />{t('全部站点')}</Link> : <span className="watch-help">{t('按站点浏览，选择作品播放或下载。')}</span>}<Link className="button button-secondary button-small" to="/downloads?view=web"><Download aria-hidden="true" />{t('批量下载')}</Link></div>
      <form className="watch-search" onSubmit={event => { event.preventDefault(); setParams(routeParams({ ...(selectedSource ? { source: selectedSource } : {}), ...(draft.trim() ? { q: draft.trim() } : {}) })) }}>
        <label className="sr-only" htmlFor="watch-query">{t('搜索番号、作品名或演员')}</label><Search aria-hidden="true" /><input id="watch-query" value={draft} maxLength={120} onChange={event => setDraft(event.target.value)} placeholder={t('搜索番号、作品名或演员')} /><Button type="submit" variant="primary">{t('检索站点')}</Button>
      </form>
      <nav className="watch-source-tabs" aria-label={t('Web 站点')}><Link className={`button button-small ${!selectedSource ? 'button-primary' : 'button-ghost'}`} aria-current={!selectedSource ? 'page' : undefined} to={href()}>{t('全部站点')}</Link>{enabled.map(site => <Link key={site.id} className={`button button-small ${selectedSource === site.id ? 'button-primary' : 'button-ghost'}`} aria-current={selectedSource === site.id ? 'page' : undefined} to={href(site.id)}>{site.name}</Link>)}</nav>
      {sites.isPending ? <SkeletonRows /> : sites.isError ? <InlineNotice tone="danger">{sites.error.message} <Button onClick={() => void sites.refetch()}>{t('重试')}</Button></InlineNotice> : !active.length ? <EmptyState title={t('没有启用的站点')} action={<Link className="button button-primary" to="/sites">{t('前往站点设置')}</Link>} /> : selectedSource ? active.map(site => <SiteResults key={`${site.id}:${query}`} site={site} query={query} />) : active.map(site => <SiteShelf key={`${site.id}:${query}`} site={site} query={query} href={href(site.id)} />)}
    </>}
  </div>
}

export function WatchPage() {
  return <div className="page watch-page"><PageHeader title={t('在线观看')} description={t('按站点浏览，选择作品播放或下载。')} actions={<><Link className="button button-secondary button-small" to="/favorites"><Bookmark aria-hidden="true" />{t('收藏')}</Link><Link className="button button-secondary button-small" to="/sites"><Settings aria-hidden="true" />{t('管理站点')}</Link></>} /><WebCatalogWorkspace /></div>
}
