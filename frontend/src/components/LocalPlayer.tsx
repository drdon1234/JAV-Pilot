import { useEffect, useRef, useState } from 'react'
import { Link } from 'react-router-dom'
import { useQuery } from '@tanstack/react-query'
import { requestJson } from '../lib/api'
import { savePlaybackProgress, waitForPlaybackSaves } from '../lib/playbackProgress'
import { usePlaybackActivity } from '../lib/playbackActivity'
import { t } from '../lib/i18n'
import type { MediaLibraryEntry } from '../types'
import { Button, InlineNotice, StatusBadge } from './ui'
import { VideoControls } from './VideoControls'
import '../styles/localPlayback.css'
import type { SubtitleSource } from '../lib/playbackSubtitles'

interface LocalPlayback {
  entry: MediaLibraryEntry
  file: number
  identity: string
  position: number
  media_url: string
  subtitles?: SubtitleSource[]
}

export function LocalPlayer({ entry, file }: { entry: string; file: number }) {
  const query = useQuery({
    queryKey: ['local-playback', entry, file],
    queryFn: async ({ signal }) => { await waitForPlaybackSaves(); return requestJson<LocalPlayback>(`/api/library/playback?${new URLSearchParams({ entry, file: String(file) })}`, { signal }) },
    staleTime: 0, gcTime: 0, refetchOnWindowFocus: false, retry: false,
  })
  return <section className="local-playback">
    {query.isPending || query.isFetching ? <p role="status">{t('正在加载')}</p> : query.isError ? <InlineNotice tone="danger">{t('本地文件不可用，请检查存储连接后重试。')} <Button onClick={() => void query.refetch()}>{t('重试')}</Button></InlineNotice> : null}
    {query.data && !query.isFetching ? <LocalVideo key={`${entry}:${file}:${query.data.identity}`} playback={query.data} /> : null}
  </section>
}

function LocalVideo({ playback }: { playback: LocalPlayback }) {
  const activity = usePlaybackActivity()
  const video = useRef<HTMLVideoElement>(null)
  const ready = useRef(false)
  const [error, setError] = useState(false)
  const [saveError, setSaveError] = useState(false)
  const [autoplayBlocked, setAutoplayBlocked] = useState(false)
  const { entry, file, identity, position, media_url } = playback
  const saveRef = useRef<() => void>(() => undefined)
  useEffect(() => {
    const element = video.current!
    let lastSaved = -1
    let disposed = false
    const save = () => {
      if (!ready.current || !Number.isFinite(element.currentTime)) return
      const current = element.ended ? 0 : element.currentTime
      if (current === lastSaved) return
      lastSaved = current
      const body = JSON.stringify({ entry: entry.entry_id, file, identity, position: current, duration: Number.isFinite(element.duration) ? element.duration : 0, completed: element.ended })
      void savePlaybackProgress('/api/library/playback/progress', body).then(() => {
        if (!disposed) setSaveError(false)
      }).catch(() => {
          lastSaved = -1
          if (!disposed) setSaveError(true)
      })
    }
    saveRef.current = save
    const interval = window.setInterval(save, 10_000)
    const onVisibility = () => { if (document.visibilityState === 'hidden') save() }
    element.addEventListener('pause', save)
    element.addEventListener('seeked', save)
    element.addEventListener('ended', save)
    window.addEventListener('pagehide', save)
    document.addEventListener('visibilitychange', onVisibility)
    return () => {
      save()
      disposed = true
      window.clearInterval(interval)
      element.removeEventListener('pause', save)
      element.removeEventListener('seeked', save)
      element.removeEventListener('ended', save)
      window.removeEventListener('pagehide', save)
      document.removeEventListener('visibilitychange', onVisibility)
      element.pause()
      element.removeAttribute('src')
      element.load()
    }
  }, [entry.entry_id, file, identity])
  return <>
    <section className="watch-player" aria-label={t('本地播放')}>
      <VideoControls video={video} subtitles={playback.subtitles} resolution={entry.quality_height ? <span className="video-resolution">{entry.quality_height}p</span> : undefined}>
      <video ref={video} src={media_url} poster={activity.snapshot?.image || undefined} controls playsInline preload="auto" aria-label={entry.code || t('本地播放')}
        onLoadedMetadata={() => {
          const element = video.current!
          if (ready.current) return
          ready.current = true
          if (position > 0 && Number.isFinite(element.duration)) element.currentTime = Math.min(position, Math.max(0, element.duration - 1))
          if (activity.current.current && activity.autoplay.current && !document.hidden) void element.play().catch(() => setAutoplayBlocked(true))
        }}
        onPlay={() => setAutoplayBlocked(false)} onError={() => setError(true)} />
      </VideoControls>
      <div className="watch-player-toolbar">
        <p className="watch-help">{t('播放进度自动保存，下次打开继续观看。')}</p>
        <Button onClick={() => { if (video.current) { video.current.currentTime = 0; saveRef.current() } }}>{t('从头播放')}</Button>
      </div>
      {autoplayBlocked ? <p role="status">{t('播放已就绪，点击播放器开始观看。')}</p> : null}
      {error ? <InlineNotice tone="danger">{t('浏览器无法播放此文件。请使用支持该编码的浏览器，或将文件转换为 H.264/AAC MP4。')}</InlineNotice> : null}
      {saveError ? <InlineNotice tone="danger">{t('播放进度保存失败，请检查网络。')} <Button onClick={() => saveRef.current()}>{t('重试')}</Button></InlineNotice> : null}
    </section>
    <div className="local-playback-heading">
      <h2>{entry.code || t('本地媒体')}{entry.title ? ` · ${entry.title}` : ''}</h2>
      <div className="watch-inline-actions"><StatusBadge tone="info">{t('本地播放')}</StatusBadge>{entry.quality_height ? <StatusBadge>{entry.quality_height}p</StatusBadge> : null}</div>
    </div>
    <div className="local-playback-files" aria-label={t('视频文件')}>
      {entry.media_paths.map((path, index) => <Link key={path} className={`local-playback-file${index === file ? ' is-active' : ''}`}
        aria-current={index === file ? 'true' : undefined} to={`/player?${new URLSearchParams({ entry: entry.entry_id, file: String(index) })}`}>
        <strong>{t('视频文件')} {index + 1}</strong><span>{path.split('/').pop()}</span>
      </Link>)}
    </div>
  </>
}
