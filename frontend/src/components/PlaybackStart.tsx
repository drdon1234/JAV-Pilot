import { watchCoverUrl } from '../lib/watching'
import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { ArrowRight, Bookmark, Film, Library, Play } from 'lucide-react'
import { requestJson } from '../lib/api'
import { waitForPlaybackSaves } from '../lib/playbackProgress'
import { t } from '../lib/i18n'
import { playbackTime } from './VideoControls'
import { Button, InlineNotice, SkeletonRows } from './ui'
import type { PlaybackRecord } from '../pages/PlaybackHistoryPage'

export function PlaybackStart() {
  const history = useQuery({ queryKey: ['playback-history'], queryFn: async () => { await waitForPlaybackSaves(); return requestJson<{ items: PlaybackRecord[] }>('/api/playback-history') }, staleTime: 0, refetchOnMount: 'always' })
  const items = history.data?.items.slice(0, 6) ?? []
  return <div className="playback-start">
    <div className="playback-start-intro"><h2>{t('选择作品开始观看')}</h2><p>{t('播放后自动记录进度，可随时继续观看。')}</p></div>
    <nav className="playback-shortcuts" aria-label={t('选择播放来源')}>
      {[{ to: '/library', label: t('媒体库'), description: t('浏览已归档的本地作品'), icon: Library }, { to: '/watch', label: t('在线观看'), description: t('搜索启用站点中的作品'), icon: Play }, { to: '/favorites', label: t('收藏'), description: t('打开收藏的作品与播放链接'), icon: Bookmark }].map(({ to, label, description, icon: Icon }) => <Link key={to} to={to}><Icon aria-hidden="true" /><span><strong>{label}</strong><small>{description}</small></span><ArrowRight aria-hidden="true" /></Link>)}
    </nav>
    <section className="playback-recent" aria-labelledby="playback-recent-heading">
      <div className="playback-recent-heading"><h2 id="playback-recent-heading">{t('最近观看')}</h2><Link className="button button-ghost button-small" to="/history">{t('全部记录')}<ArrowRight aria-hidden="true" /></Link></div>
      {history.isPending ? <SkeletonRows count={2} /> : history.isError ? <InlineNotice tone="danger">{t('无法加载播放记录，请重试。')}<Button onClick={() => void history.refetch()}>{t('重试')}</Button></InlineNotice> : items.length ? <div className="playback-recent-grid">{items.map(item => <Link key={item.id} className="playback-recent-item" to={`/player?${new URLSearchParams(item.kind === 'local' ? { entry: item.entry, file: String(item.file) } : { source: item.source, url: item.url })}`}>
        <div className="playback-recent-image">{item.cover ? <img src={watchCoverUrl(item.source, item.cover)} alt="" loading="lazy" referrerPolicy="no-referrer" onError={event => { const image = event.currentTarget; if (item.kind === 'local' && !image.dataset.fallback) { image.dataset.fallback = '1'; image.src = `/api/library/artwork?entry=${encodeURIComponent(item.entry)}` } else image.hidden = true }} /> : null}<Film aria-hidden="true" /><span><Play aria-hidden="true" />{item.completed ? t('从头播放') : t('继续播放')}</span></div>
        <strong>{item.code || item.title}</strong><span className="playback-recent-title">{item.title}</span>
        <progress aria-label={t('播放进度')} max={item.duration || 1} value={item.completed ? item.duration : Math.min(item.position, item.duration)} />
        <small>{item.kind === 'local' ? t('本地播放') : item.source} · {playbackTime(item.position)} / {playbackTime(item.duration)}</small>
      </Link>)}</div> : <p className="playback-no-history">{t('暂无播放记录')}</p>}
    </section>
  </div>
}
