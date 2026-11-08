import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject, type PointerEvent } from 'react'
import { Expand, Minimize, Pause, Play, RotateCcw, RotateCw, Sun, Volume2, VolumeX } from 'lucide-react'
import { t } from '../lib/i18n'
import { usePlaybackActivity } from '../lib/playbackActivity'
import { PlayerChoice } from './PlayerMenu'
import { PlayerSubtitles } from './PlayerSubtitles'
import type { SubtitleSource } from '../lib/playbackSubtitles'

export function playbackTime(value: number) {
  if (!Number.isFinite(value)) return '0:00'
  const seconds = Math.max(0, Math.floor(value))
  const minutes = Math.floor(seconds / 60)
  return `${minutes > 59 ? `${Math.floor(minutes / 60)}:` : ''}${minutes > 59 ? String(minutes % 60).padStart(2, '0') : minutes}:${String(seconds % 60).padStart(2, '0')}`
}

export function VideoControls({ video, children, resolution, subtitles }: { video: RefObject<HTMLVideoElement | null>; children: ReactNode; resolution?: ReactNode; subtitles?: SubtitleSource[] }) {
  const stage = useRef<HTMLDivElement>(null)
  const activity = usePlaybackActivity()
  useLayoutEffect(() => {
    if (activity.active || !video.current) return
    const element = video.current
    activity.autoplay.current = false
    element.pause()
    let image = ''
    if (element.videoWidth && element.readyState >= 2) {
      try {
        const canvas = document.createElement('canvas')
        canvas.width = Math.round(element.videoWidth * Math.min(1, 960 / element.videoWidth, 540 / element.videoHeight))
        canvas.height = Math.round(canvas.width * element.videoHeight / element.videoWidth)
        canvas.getContext('2d')?.drawImage(element, 0, 0, canvas.width, canvas.height)
        image = canvas.toDataURL('image/jpeg', 0.75)
      } catch { /* Keep the previous frame if this browser cannot capture it. */ }
    }
    activity.capture({ image, position: element.currentTime, duration: Number.isFinite(element.duration) ? element.duration : 0, aspect: element.videoWidth && element.videoHeight ? element.videoWidth / element.videoHeight : 16 / 9 })
  }, [activity.active, video, activity.autoplay, activity.capture])
  const timer = useRef<number | undefined>(undefined)
  const clickTimer = useRef<number | undefined>(undefined)
  const hintTimer = useRef<number | undefined>(undefined)
  const lastPointer = useRef('mouse')
  const touch = useRef<{ id: number; x: number; y: number; zone: number; volume: number; brightness: number; dragging: boolean } | null>(null)
  const tap = useRef<{ time: number; zone: number } | null>(null)
  const brightness = useRef(1)
  const [show, setShow] = useState(true)
  const [hint, setHint] = useState<{ kind: 'brightness' | 'volume' | 'seek'; text: string } | null>(null)
  const [state, setState] = useState({ paused: true, time: 0, duration: 0, muted: false, speed: 1, fullscreen: false })
  const seeking = useRef<number | null>(null)
  const [target, setTarget] = useState<number | null>(null)
  const [buffering, setBuffering] = useState(false)
  useEffect(() => {
    const element = stage.current!
    const observer = new ResizeObserver(() => element.style.setProperty('--video-stage-height', `${element.clientHeight}px`))
    observer.observe(element)
    return () => observer.disconnect()
  }, [])
  const reveal = () => {
    setShow(true)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => { if (!video.current?.paused) setShow(false) }, 3500)
  }
  const feedback = (kind: 'brightness' | 'volume' | 'seek', text: string) => {
    setHint({ kind, text })
    window.clearTimeout(hintTimer.current)
    hintTimer.current = window.setTimeout(() => setHint(null), 1000)
  }
  useEffect(() => {
    const element = video.current!
    element.controls = false
    const update = () => {
      if (!activity.current.current && !element.paused) element.pause()
      setState({ paused: element.paused, time: seeking.current ?? element.currentTime, duration: Number.isFinite(element.duration) ? element.duration : 0, muted: element.muted, speed: element.playbackRate, fullscreen: Boolean(document.fullscreenElement) })
    }
    const pauseHidden = () => { if (document.hidden) { activity.autoplay.current = false; element.pause() } }
    const pause = () => element.pause()
    const onSeeking = () => setBuffering(true)
    const onSeeked = () => { seeking.current = null; setTarget(null); setBuffering(false); update() }
    const onReady = () => { if (!element.seeking) setBuffering(false) }
    const events = ['timeupdate', 'durationchange', 'play', 'pause', 'volumechange', 'ratechange', 'loadedmetadata']
    events.forEach(event => element.addEventListener(event, update))
    element.addEventListener('seeking', onSeeking)
    element.addEventListener('waiting', onSeeking)
    element.addEventListener('seeked', onSeeked)
    element.addEventListener('canplay', onReady)
    element.addEventListener('playing', onReady)
    element.addEventListener('error', onSeeked)
    element.addEventListener('play', reveal)
    document.addEventListener('fullscreenchange', update)
    document.addEventListener('visibilitychange', pauseHidden)
    window.addEventListener('pagehide', pause)
    update()
    return () => {
      events.forEach(event => element.removeEventListener(event, update))
      element.removeEventListener('seeking', onSeeking)
      element.removeEventListener('waiting', onSeeking)
      element.removeEventListener('seeked', onSeeked)
      element.removeEventListener('canplay', onReady)
      element.removeEventListener('playing', onReady)
      element.removeEventListener('error', onSeeked)
      element.removeEventListener('play', reveal)
      document.removeEventListener('fullscreenchange', update)
      document.removeEventListener('visibilitychange', pauseHidden)
      window.removeEventListener('pagehide', pause)
      for (const timerRef of [timer, clickTimer, hintTimer]) window.clearTimeout(timerRef.current)
    }
  }, [video])
  const toggle = () => {
    const element = video.current!
    if (element.paused) { activity.autoplay.current = true; void element.play().catch(() => undefined) }
    else element.pause()
    reveal()
  }
  const seekTo = (position: number) => {
    const element = video.current!
    if (!Number.isFinite(element.duration)) return
    const next = Math.max(0, Math.min(element.duration, position))
    seeking.current = next; setTarget(next); setBuffering(true)
    element.currentTime = next
    reveal()
  }
  const seek = (offset: number) => {
    const element = video.current!
    if (Number.isFinite(element.duration)) seekTo((seeking.current ?? element.currentTime) + offset)
    feedback('seek', offset < 0 ? t('后退 30 秒') : t('前进 30 秒'))
    reveal()
  }
  const fullscreen = () => {
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined)
    else if (stage.current?.requestFullscreen) void stage.current.requestFullscreen().catch(() => undefined)
    else (video.current as HTMLVideoElement & { webkitEnterFullscreen?: () => void })?.webkitEnterFullscreen?.()
    reveal()
  }
  const pointerDown = (event: PointerEvent<HTMLDivElement>) => {
    lastPointer.current = event.pointerType
    if (event.pointerType === 'mouse') { touch.current = null; return }
    if (!event.isPrimary) { touch.current = null; tap.current = null; return }
    const bounds = event.currentTarget.getBoundingClientRect()
    const ratio = (event.clientX - bounds.left) / bounds.width
    touch.current = { id: event.pointerId, x: event.clientX, y: event.clientY, zone: ratio < 0.33 ? -1 : ratio > 0.67 ? 1 : 0, volume: video.current!.volume, brightness: brightness.current, dragging: false }
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const pointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (event.pointerType === 'mouse') { reveal(); return }
    const start = touch.current
    if (!start || start.id !== event.pointerId) return
    const dy = start.y - event.clientY
    if (!start.dragging && Math.hypot(dy, start.x - event.clientX) > 12) {
      start.dragging = true
      tap.current = null
      window.clearTimeout(clickTimer.current)
    }
    if (!start.dragging || !start.zone) return
    event.preventDefault()
    const delta = dy / Math.max(100, event.currentTarget.clientHeight) * 1.5
    if (start.zone < 0) {
      brightness.current = Math.max(0.2, Math.min(1.5, start.brightness + delta))
      video.current!.style.filter = `brightness(${brightness.current})`
      feedback('brightness', `${t('画面亮度')} ${Math.round(brightness.current * 100)}%`)
    } else {
      const volume = Math.max(0, Math.min(1, start.volume + delta))
      video.current!.volume = volume
      video.current!.muted = volume === 0
      feedback('volume', `${t('音量')} ${Math.round(volume * 100)}%`)
    }
  }
  const pointerUp = (event: PointerEvent<HTMLDivElement>) => {
    const start = touch.current
    touch.current = null
    if (event.pointerType === 'mouse' || !start || start.id !== event.pointerId || start.dragging) return
    event.preventDefault()
    const previous = tap.current
    const now = performance.now()
    if (previous && now - previous.time < 320 && previous.zone === start.zone) {
      tap.current = null
      window.clearTimeout(clickTimer.current)
      if (start.zone) seek(start.zone * 30)
      else toggle()
    } else {
      tap.current = { time: now, zone: start.zone }
      window.clearTimeout(clickTimer.current)
      clickTimer.current = window.setTimeout(() => { tap.current = null; if (show && !video.current?.paused) setShow(false); else reveal() }, 320)
    }
  }
  return <div ref={stage} className={`video-stage${show || state.paused ? ' controls-visible' : ''}`} tabIndex={0} aria-label={t('应用内播放器')}
    onKeyDown={event => {
      if (event.target !== stage.current && event.target !== video.current) return
      if (event.key === ' ' || event.key === 'k') { event.preventDefault(); toggle() }
      if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') { event.preventDefault(); seek(event.key === 'ArrowLeft' ? -30 : 30) }
      if (event.key === 'f') fullscreen()
    }}>
    {children}
    {buffering ? <div className="video-buffering" role="status" aria-label={t('正在加载')}><span className="video-buffering-spinner" /></div> : null}
    <div className="video-gesture-surface" aria-hidden="true" onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp} onPointerCancel={() => { touch.current = null; tap.current = null; window.clearTimeout(clickTimer.current) }}
      onClick={() => { if (lastPointer.current !== 'mouse') return; window.clearTimeout(clickTimer.current); clickTimer.current = window.setTimeout(toggle, 250) }}
      onDoubleClick={() => { if (lastPointer.current !== 'mouse') return; window.clearTimeout(clickTimer.current); fullscreen() }} />
    {hint ? <div className="video-gesture-hint" role="status">{hint.kind === 'brightness' ? <Sun aria-hidden="true" /> : hint.kind === 'volume' ? <Volume2 aria-hidden="true" /> : null}{hint.text}</div> : null}
    <div className="video-controls" onPointerDown={reveal} onFocus={reveal}>
      <div className="video-progress-row"><span>{playbackTime(target ?? state.time)}</span>
        <input aria-label={t('播放进度')} type="range" min={0} max={state.duration || 1} step={0.1} value={Math.min(target ?? state.time, state.duration || 1)} disabled={!state.duration}
          onChange={event => seekTo(Number(event.target.value))} />
        <span>{playbackTime(state.duration)}</span></div>
      <div className="video-controls-row">
        <button type="button" aria-label={state.paused ? t('播放') : t('暂停')} onClick={toggle}>{state.paused ? <Play /> : <Pause />}</button>
        <button type="button" aria-label={t('后退 30 秒')} onClick={() => seek(-30)}><RotateCcw /></button>
        <button type="button" aria-label={t('前进 30 秒')} onClick={() => seek(30)}><RotateCw /></button>
        <button className="video-volume" type="button" aria-label={state.muted ? t('取消静音') : t('静音')} onClick={() => { video.current!.muted = !video.current!.muted }}>{state.muted ? <VolumeX /> : <Volume2 />}</button>
        <div className="video-primary-options"><PlayerSubtitles video={video} sources={subtitles} />{resolution}<PlayerChoice label={t('播放速度')} value={state.speed} onChange={speed => { video.current!.playbackRate = speed }} options={[0.5, 0.75, 1, 1.25, 1.5, 2].map(speed => ({ value: speed, label: `${speed}×` }))} /></div>
        <button type="button" aria-label={state.fullscreen ? t('退出全屏') : t('全屏')} onClick={fullscreen}>{state.fullscreen ? <Minimize /> : <Expand />}</button>
      </div>
    </div>
  </div>
}
