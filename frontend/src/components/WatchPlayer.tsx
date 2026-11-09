import { useEffect, useRef, useState } from 'react'
import { RefreshCw } from 'lucide-react'

import { watching, type WatchEntry } from '../lib/watching'
import { t } from '../lib/i18n'
import { BookmarkButton } from './WatchActions'
import { Button, InlineNotice, StatusBadge } from './ui'
import { requestJson } from '../lib/api'
import { savePlaybackProgress, waitForPlaybackSaves } from '../lib/playbackProgress'
import { usePlaybackActivity } from '../lib/playbackActivity'
import { VideoControls } from './VideoControls'
import { PlayerChoice } from './PlayerMenu'

export function WatchPlayer({ entry }: { entry: WatchEntry }) {
  const activity = usePlaybackActivity()
  const video = useRef<HTMLVideoElement>(null)
  const [attempt, setAttempt] = useState(0)
  const [status, setStatus] = useState('正在解析播放线路；访问验证会在后台进行。')
  const [error, setError] = useState('')
  const [height, setHeight] = useState<number | null>(null)
  const [qualities, setQualities] = useState<{ index: number; height: number }[]>([])
  const [quality, setQuality] = useState(-1)
  const hlsRef = useRef<import('hls.js').default | null>(null)
  const resume = useRef(0)
  const initialized = useRef(false)
  const [saveError, setSaveError] = useState(false)
  useEffect(() => {
    if (activity.active) hlsRef.current?.startLoad(-1)
    else { video.current?.pause(); hlsRef.current?.stopLoad() }
  }, [activity.active])
  useEffect(() => {
    const element = video.current!
    let lastSaved = -1
    let disposed = false
    const save = () => {
      if (!initialized.current || !Number.isFinite(element.duration) || element.currentTime <= 0 && !element.ended) return
      const position = element.ended ? 0 : element.currentTime
      if (position === lastSaved) return
      lastSaved = position
      resume.current = position
      const body = JSON.stringify({ source: entry.source, url: entry.url, title: entry.title, code: entry.code, cover: entry.cover, position, duration: element.duration, completed: element.ended })
      void savePlaybackProgress('/api/playback-history/action', body).then(() => {
        if (!disposed) setSaveError(false)
      }).catch(() => {
        lastSaved = -1; if (!disposed) setSaveError(true)
      })
    }
    const interval = window.setInterval(save, 10_000)
    const visibility = () => { if (document.visibilityState === 'hidden') save() }
    for (const event of ['pause', 'seeked', 'ended']) element.addEventListener(event, save)
    window.addEventListener('pagehide', save)
    document.addEventListener('visibilitychange', visibility)
    return () => {
      save(); disposed = true; window.clearInterval(interval)
      for (const event of ['pause', 'seeked', 'ended']) element.removeEventListener(event, save)
      window.removeEventListener('pagehide', save)
      document.removeEventListener('visibilitychange', visibility)
    }
  }, [entry.source, entry.url])
  useEffect(() => {
    let disposed = false
    let session = ''
    let onMetadata: (() => void) | undefined
    const element = video.current
    initialized.current = false
    setError('')
    setStatus(t('正在解析播放线路；访问验证会在后台进行。'))
    setQualities([])
    setHeight(null)
    setQuality(-1)
    void (async () => {
      try {
        // Let an in-flight resolve finish so navigation can close its session.
        const [playback, history] = await Promise.all([watching.resolve(entry), waitForPlaybackSaves().then(() => requestJson<{ items: { position: number }[] }>(`/api/playback-history?${new URLSearchParams({ source: entry.source, url: entry.url })}`)).catch(() => null)])
        if (!attempt) resume.current = history?.items[0]?.position ?? 0
        session = playback.session
        if (disposed || !element) { void watching.close(session).catch(() => undefined); return }
        setHeight(playback.height)
        const start = () => {
          setStatus('')
          const restore = () => {
            if (disposed || initialized.current) return
            initialized.current = true
            if (resume.current > 0 && Number.isFinite(element.duration)) element.currentTime = Math.min(resume.current, Math.max(0, element.duration - 1))
          }
          if (element.readyState >= 1) restore()
          else { onMetadata = restore; element.addEventListener('loadedmetadata', restore, { once: true }) }
          if (activity.current.current && activity.autoplay.current && !document.hidden) void element.play().catch(() => setStatus(t('播放已就绪，点击播放器开始观看。')))
          else if (!activity.current.current) hlsRef.current?.stopLoad()
        }
        const { default: Hls } = await import('hls.js')
        if (disposed) return
        if (playback.media_kind === 'progressive' || !Hls.isSupported() && element.canPlayType('application/vnd.apple.mpegurl')) {
          element.src = playback.media_url
          onMetadata = start
          element.addEventListener('loadedmetadata', start, { once: true })
          element.load()
        } else {
          if (!Hls.isSupported()) throw new Error(t('此浏览器不支持 HLS 播放，请使用新版 Chrome、Edge 或 Safari。'))
          const hls = new Hls({ enableWorker: false, backBufferLength: 90, maxBufferLength: 60, maxMaxBufferLength: 120, maxBufferSize: 64 * 1024 * 1024 })
          hlsRef.current = hls
          hls.on(Hls.Events.MANIFEST_PARSED, (_event, data) => {
            setQualities(data.levels.map((level, index) => ({ index, height: level.height })).filter(level => level.height > 0))
            start()
          })
          hls.on(Hls.Events.ERROR, (_event, data) => {
            if (data.fatal) {
              let message = t('播放中断或链接已过期。请重新解析，或返回结果选择其他站点。')
              try { message = JSON.parse(String(data.response?.data)).error || message } catch { /* The upstream may not return JSON. */ }
              setError(message)
              setStatus('')
              hls.destroy()
            }
          })
          hls.loadSource(playback.media_url)
          hls.attachMedia(element)
        }
      } catch (failure) {
        if (!disposed) { setError((failure as Error).message); setStatus('') }
      }
    })()
    return () => {
      disposed = true
      hlsRef.current?.destroy()
      hlsRef.current = null
      if (element) {
        if (onMetadata) element.removeEventListener('loadedmetadata', onMetadata)
        element.pause(); element.removeAttribute('src'); element.load()
      }
      if (session) void watching.close(session).catch(() => undefined)
    }
  }, [entry.source, entry.url, attempt])

  return <section className="watch-player" aria-label={t('应用内播放器')}>
    <VideoControls video={video} resolution={qualities.length > 1 ? <PlayerChoice label={t('清晰度')} value={quality} onChange={selected => {
      setQuality(selected)
      if (hlsRef.current) hlsRef.current.currentLevel = selected
    }} options={[{ value: -1, label: t('自动') }, ...qualities.map(level => ({ value: level.index, label: `${level.height}p` }))]} /> : height ? <span className="video-resolution">{height}p</span> : undefined}>
    <video ref={video} poster={activity.snapshot?.image || undefined} controls={false} playsInline preload="auto" aria-label={entry.title || t('在线观看')}
      onError={() => { setError(t('播放资源无法加载，请重新解析或切换站点。')); setStatus('') }} />
    </VideoControls>
    <div className="watch-player-toolbar">
      <div className="watch-inline-actions">
        <StatusBadge tone="info">{t('应用内播放')}</StatusBadge>
      </div>
      <div className="watch-inline-actions">
        <BookmarkButton entry={entry} kind="playback" />
        <Button type="button" size="small" onClick={() => setAttempt(value => value + 1)}><RefreshCw aria-hidden="true" />{t('重新解析')}</Button>
      </div>
    </div>
    {status ? <p className="watch-player-status" role="status">{status}</p> : null}
    {error ? <InlineNotice tone="danger" role="alert">{error}</InlineNotice> : null}
    {saveError ? <InlineNotice tone="warning">{t('播放进度保存失败，请检查网络。')}</InlineNotice> : null}
  </section>
}
