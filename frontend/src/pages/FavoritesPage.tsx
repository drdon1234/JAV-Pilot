import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ArrowRight, Bookmark as BookmarkIcon, Play, Search, Trash2 } from 'lucide-react'
import { useState } from 'react'
import { Link } from 'react-router-dom'

import { useToast } from '../components/ToastProvider'
import { Button, EmptyState, InlineNotice, PageHeader, SkeletonRows, StatusBadge } from '../components/ui'
import { bookmarkLabels, editionLabel, watching, watchHref, type Bookmark, type BookmarkKind } from '../lib/watching'
import { t } from '../lib/i18n'
import { StableImage } from './WorkUi'
import { requestJson } from '../lib/api'
import { useSelection, SelectionTools, SelectionCheck, RemoveSelection } from '../components/SelectionTools'

export function FavoritesPage() {
  const [kind, setKind] = useState<BookmarkKind | ''>('')
  const [query, setQuery] = useState('')
  const client = useQueryClient()
  const selection = useSelection(`${kind}:${query}`)
  const toast = useToast()
  const bookmarks = useQuery({ queryKey: ['bookmarks'], queryFn: watching.bookmarks })
  const remove = useMutation({
    mutationFn: watching.remove,
    onSuccess: () => void client.invalidateQueries({ queryKey: ['bookmarks'] }),
    onError: (error: Error) => toast.push(error.message, 'error'),
  })
  const items = bookmarks.data?.items ?? []
  const bulk = useMutation({ mutationFn: (ids: string[]) => requestJson('/api/bookmarks/action', { method: 'POST', body: JSON.stringify({ action: 'remove', ids }) }), onSuccess: () => { selection.finish(); void client.invalidateQueries({ queryKey: ['bookmarks'] }) }, onError: (error: Error) => toast.push(error.message, 'error') })
  const filtered = items.filter(item => (!kind || item.kind === kind) && `${item.title} ${item.code} ${item.source} ${item.editions.join(' ')}`.toLowerCase().includes(query.toLowerCase()))
  return <div className="page watch-page">
    <PageHeader title={t('收藏')} description={t('资料页用于查看信息，站点作品进入作品页，播放链接直接开始解析播放。')}
      actions={<Link className="button button-secondary button-small" to="/watch"><Play aria-hidden="true" />{t('在线观看')}</Link>} />
    <div className="watch-favorite-tools">
      <nav className="watch-source-tabs" aria-label={t('收藏类型')}>
        <Button type="button" aria-pressed={!kind} variant={!kind ? 'primary' : 'ghost'} onClick={() => setKind('')}>{t('全部收藏')} <span>{items.length}</span></Button>
        {(Object.entries(bookmarkLabels) as [BookmarkKind, string][]).map(([value, label]) => <Button key={value} type="button" variant={kind === value ? 'primary' : 'ghost'} aria-pressed={kind === value}
          onClick={() => setKind(value)}>{label} <span>{items.filter(item => item.kind === value).length}</span></Button>)}
      </nav>
      <label className="watch-filter"><Search aria-hidden="true" /><span className="sr-only">{t('筛选收藏')}</span><input value={query} onChange={event => setQuery(event.target.value)} placeholder={t('筛选收藏')} /></label>
    </div>
    <SelectionTools selection={selection} ids={filtered.map(item => item.id)} busy={bulk.isPending}><RemoveSelection count={selection.selected.size} busy={bulk.isPending} onRemove={() => bulk.mutate([...selection.selected])} /></SelectionTools>
    {bookmarks.isPending ? <SkeletonRows /> : bookmarks.isError ? <InlineNotice tone="danger" role="alert">{bookmarks.error.message}
      <Button type="button" onClick={() => void bookmarks.refetch()}>{t('重试')}</Button></InlineNotice>
      : !filtered.length ? <EmptyState title={items.length ? t('没有符合条件的收藏') : t('还没有收藏')}
        description={items.length ? t('尝试其他关键词或收藏类型。') : t('在作品详情页收藏资料，或在在线观看中保存作品与播放入口。')}
        action={<Link className="button button-primary" to="/watch"><BookmarkIcon aria-hidden="true" />{t('浏览在线观看')}</Link>} />
        : <div className="watch-entry-list">{filtered.map(item => <div key={item.id} className={selection.managing ? 'selection-row' : undefined}><SelectionCheck selection={selection} id={item.id} name={item.title} disabled={bulk.isPending} /><FavoriteRow item={item} removing={remove.isPending && remove.variables === item.id} onRemove={() => remove.mutate(item.id)} /></div>)}</div>}
  </div>
}

function FavoriteRow({ item, removing, onRemove }: { item: Bookmark; removing: boolean; onRemove: () => void }) {
  const destination = item.kind === 'detail' ? item.url : watchHref(item, item.kind === 'playback')
  return <article className="watch-entry">
    <Link className="watch-entry-cover" to={destination} tabIndex={-1} aria-hidden="true"><StableImage src={item.cover} alt="" loading="lazy" referrerPolicy="no-referrer" /></Link>
    <div className="watch-entry-body"><div className="watch-inline-actions"><strong>{item.code}</strong><StatusBadge tone={item.kind === 'playback' ? 'info' : 'neutral'}>{bookmarkLabels[item.kind]}</StatusBadge>
      {item.editions.map(edition => <StatusBadge key={edition}>{editionLabel(edition)}</StatusBadge>)}</div>
      <h3><Link to={destination}>{item.title}</Link></h3>
      <span className="watch-help">{item.source || t('作品资料')} · {new Date(item.created_at * 1000).toLocaleDateString()}</span>
    </div>
    <div className="watch-entry-actions"><Link className={`button button-small ${item.kind === 'playback' ? 'button-primary' : 'button-secondary'}`} to={destination}>
      {item.kind === 'playback' ? <Play aria-hidden="true" /> : <ArrowRight aria-hidden="true" />}{item.kind === 'playback' ? t('直接播放') : item.kind === 'detail' ? t('查看资料') : t('进入作品')}</Link>
      <Button type="button" size="small" variant="ghost" onClick={onRemove} disabled={removing} aria-label={t('取消收藏 {title}', { title: item.title })}><Trash2 aria-hidden="true" />{t('取消收藏')}</Button></div>
  </article>
}
