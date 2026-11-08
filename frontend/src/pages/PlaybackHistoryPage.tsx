import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Film, Play, RefreshCw, Trash2 } from 'lucide-react'
import { HistoryTabs } from '../components/HistoryTabs'
import { playbackTime } from '../components/VideoControls'
import { Button, EmptyState, IconButton, InlineNotice, PageHeader, SkeletonRows, StatusBadge } from '../components/ui'
import { requestJson } from '../lib/api'
import { waitForPlaybackSaves } from '../lib/playbackProgress'
import { formatDateTime } from '../lib/format'
import { t } from '../lib/i18n'
import { useSelection, SelectionTools, SelectionCheck, RemoveSelection } from '../components/SelectionTools'

export interface PlaybackRecord { id: string; kind: 'local' | 'online'; title: string; code: string; cover: string; source: string; url: string; entry: string; file: number; position: number; duration: number; completed: boolean; updated_at: number }

export function PlaybackHistoryPage() {
  const client = useQueryClient()
  const selection = useSelection()
  const history = useQuery({ queryKey: ['playback-history'], queryFn: async () => { await waitForPlaybackSaves(); return requestJson<{items: PlaybackRecord[]}>('/api/playback-history') }, staleTime: 0, refetchOnMount: 'always' })
  const remove = useMutation({ mutationFn: (id: string) => requestJson('/api/playback-history/action', { method: 'POST', body: JSON.stringify({ action: 'remove', id }) }), onSuccess: () => void client.invalidateQueries({ queryKey: ['playback-history'] }) })
  const bulk = useMutation({ mutationFn: (ids: string[]) => requestJson('/api/playback-history/action', { method: 'POST', body: JSON.stringify({ action: 'remove', ids }) }), onSuccess: () => { selection.finish(); void client.invalidateQueries({ queryKey: ['playback-history'] }) } })
  return <div className="page watch-page">
    <PageHeader title={t('历史记录')} actions={<IconButton label={t('刷新')} onClick={() => void history.refetch()} disabled={history.isFetching}><RefreshCw aria-hidden="true" /></IconButton>} />
    <HistoryTabs />
    <SelectionTools selection={selection} ids={history.data?.items.map(item => item.id) ?? []} busy={bulk.isPending}><RemoveSelection count={selection.selected.size} busy={bulk.isPending} onRemove={() => bulk.mutate([...selection.selected])} /></SelectionTools>
    {history.isPending ? <SkeletonRows count={3} /> : history.isError ? <InlineNotice tone="danger">{t('无法加载播放记录，请重试。')} <Button onClick={() => void history.refetch()}>{t('重试')}</Button></InlineNotice> : history.data.items.length ? <div className="playback-history-list">
      {history.data.items.map(item => {
        const target = `/player?${new URLSearchParams(item.kind === 'local' ? { entry: item.entry, file: String(item.file) } : { source: item.source, url: item.url })}`
        return <article key={item.id} className={`playback-history-row${selection.managing ? ' selection-row' : ''}`}>
          <SelectionCheck selection={selection} id={item.id} name={item.title || item.code} disabled={bulk.isPending} />
          <Link to={target} className="playback-history-cover" aria-label={item.title}>{item.cover ? <img src={item.cover} alt="" loading="lazy" referrerPolicy="no-referrer" onError={event => {
            const image = event.currentTarget
            if (item.kind === 'local' && !image.dataset.fallback) { image.dataset.fallback = '1'; image.src = `/api/library/artwork?entry=${encodeURIComponent(item.entry)}` }
            else { image.hidden = true; image.parentElement?.classList.add('cover-unavailable') }
          }} /> : null}<Film className={item.cover ? 'history-cover-fallback' : undefined} aria-hidden="true" /></Link>
          <div className="playback-history-body"><div className="watch-inline-actions"><strong>{item.code}</strong><StatusBadge>{item.kind === 'local' ? t('本地播放') : item.source}</StatusBadge></div>
            <h2><Link to={target}>{item.title || item.code}</Link></h2>
            <p>{item.completed ? t('已看完') : t('已播放')} {playbackTime(item.completed ? item.duration : item.position)} / {playbackTime(item.duration)}</p>
            <progress aria-label={t('播放进度')} max={item.duration || 1} value={item.completed ? item.duration : Math.min(item.position, item.duration)} />
            <time dateTime={new Date(item.updated_at * 1000).toISOString()}>{formatDateTime(item.updated_at)}</time>
          </div>
          <div className="playback-history-actions"><Link className="button button-primary" to={target}><Play aria-hidden="true" />{item.completed ? t('从头播放') : t('继续播放')}</Link><IconButton label={t('删除记录')} disabled={remove.isPending} onClick={() => remove.mutate(item.id)}><Trash2 aria-hidden="true" /></IconButton></div>
        </article>
      })}
    </div> : <EmptyState title={t('暂无播放记录')} description={t('播放后自动记录进度，可随时继续观看。')} action={<Link className="button button-primary" to="/library">{t('媒体库')}</Link>} />}
    {remove.isError ? <InlineNotice tone="danger">{remove.error.message}</InlineNotice> : null}
    {bulk.isError ? <InlineNotice tone="danger">{bulk.error.message}</InlineNotice> : null}
  </div>
}
