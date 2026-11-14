import { useEffect, useLayoutEffect, useRef, useState, type ReactNode, type RefObject, type PointerEvent } from 'react'
import { ArrowLeft, Expand, Minimize, Pause, Play, RotateCcw, RotateCw, SlidersHorizontal, Sun, Volume2 } from 'lucide-react'
import { t } from '../lib/i18n'
import { usePlaybackActivity } from '../lib/playbackActivity'
import { PlayerChoice, PlayerMenu } from './PlayerMenu'
import { PlayerSubtitles } from './PlayerSubtitles'
import type { SubtitleSource } from '../lib/playbackSubtitles'
import { playbackTime } from '../lib/format'
import { usePlaybackSpeedHold } from '../lib/usePlaybackSpeedHold'

const PLAYER_INTERACTIVE = 'a, button, input, select, textarea, summary, [contenteditable]:not([contenteditable="false"]), [role="button"], [role="slider"], [role="textbox"], [role="combobox"]'
const PLAYER_EDITABLE = 'input, select, textarea, [contenteditable]:not([contenteditable="false"]), [role="textbox"], [role="combobox"], dialog, [role="dialog"], [aria-modal="true"]'

export function VideoControls({ video, children, resolution, subtitles }: { video: RefObject<HTMLVideoElement | null>; children: ReactNode; resolution?: ReactNode; subtitles?: SubtitleSource[] }) {
  const stage = useRef<HTMLDivElement>(null)
  const orientationOwned = useRef(false)
  const orientationPending = useRef(false)
  const fullscreenRequested = useRef(false)
  const settingsOpen = () => Boolean(stage.current?.querySelector('details[open]'))
  const rotated = () => document.fullscreenElement === stage.current && window.matchMedia('(pointer: coarse) and (orientation: portrait)').matches
  const unlockOrientation = () => {
    if (!orientationOwned.current) return
    orientationOwned.current = false
    try { screen.orientation?.unlock() } catch { /* The browser may have already unlocked on exit. */ }
  }
  const lockLandscape = (target: HTMLDivElement) => {
    if (orientationOwned.current || orientationPending.current || (!window.matchMedia('(pointer: coarse)').matches && !navigator.maxTouchPoints)) return
    const orientation = screen.orientation as ScreenOrientation & { lock?: (direction: 'landscape') => Promise<void> }
    if (!orientation?.lock) return
    const alreadyFullscreen = document.fullscreenElement === target
    orientationPending.current = true
    let retryAfterEntry = false
    void orientation.lock('landscape').then(() => {
      if (stage.current === target && (document.fullscreenElement === target || fullscreenRequested.current)) orientationOwned.current = true
      else orientation.unlock()
    }).catch(() => {
      // Some engines require the fullscreenchange event before accepting a lock.
      retryAfterEntry = !alreadyFullscreen && document.fullscreenElement === target
    }).finally(() => {
      orientationPending.current = false
      if (retryAfterEntry) lockLandscape(target)
    })
  }
  const activity = usePlaybackActivity()
  const restoring = useRef(activity.snapshot ?? null)
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
    activity.capture({ image, position: restoring.current?.position ?? element.currentTime, duration: restoring.current?.duration || (Number.isFinite(element.duration) ? element.duration : 0), aspect: element.videoWidth && element.videoHeight ? element.videoWidth / element.videoHeight : 16 / 9 })
  }, [activity.active, video, activity.autoplay, activity.capture])
  const timer = useRef<number | undefined>(undefined)
  const clickTimer = useRef<number | undefined>(undefined)
  const hintTimer = useRef<number | undefined>(undefined)
  const lastPointer = useRef('mouse')
  const touch = useRef<{ id: number; x: number; y: number; zone: number; position: number; destination: number | null; axis: 'x' | 'y' | null; level: 'brightness' | 'volume' | null; initialLevel: number } | null>(null)
  const tap = useRef<{ time: number; zone: number } | null>(null)
  const [show, setShow] = useState(true)
  const [nativeFullscreen, setNativeFullscreen] = useState(false)
  const speedHold = usePlaybackSpeedHold(video, activity.active && !nativeFullscreen)
  const suppressClick = useRef(false)
  const mouseStart = useRef<{ x: number; y: number } | null>(null)
  const [hint, setHint] = useState<string | null>(null)
  const [state, setState] = useState({ paused: true, time: activity.snapshot?.position ?? 0, duration: activity.snapshot?.duration ?? 0, speed: 1, fullscreen: false })
  const seeking = useRef<number | null>(null)
  const [target, setTarget] = useState<number | null>(null)
  const [buffering, setBuffering] = useState(false)
  const [brightness, setBrightness] = useState(1)
  const [volume, setVolume] = useState(1)
  useEffect(() => {
    if (!activity.active || document.hidden || nativeFullscreen) return
    const focused = document.activeElement
    if (focused instanceof HTMLElement && focused.closest(PLAYER_EDITABLE)) return
    if (!stage.current?.contains(focused)) stage.current?.focus({ preventScroll: true })
  }, [activity.active, nativeFullscreen])
  const changeVolume = (value: number) => { const element = video.current; if (element) { element.volume = Math.max(0, Math.min(1, value)); element.muted = value === 0; setVolume(element.muted ? 0 : element.volume) } }
  useEffect(() => { stage.current?.style.setProperty('--playback-brightness', String(brightness)) }, [brightness])
  useEffect(() => {
    const element = video.current!
    const update = () => setVolume(element.muted ? 0 : element.volume)
    update(); element.addEventListener('volumechange', update)
    return () => element.removeEventListener('volumechange', update)
  }, [video])
  useEffect(() => {
    const element = stage.current!
    const surface = element.querySelector('.video-surface') as HTMLElement
    const observer = new ResizeObserver(() => element.style.setProperty('--video-stage-height', `${surface.clientHeight}px`))
    observer.observe(surface)
    return () => observer.disconnect()
  }, [])
  const hide = () => {
    window.clearTimeout(timer.current)
    stage.current?.querySelectorAll('details[open]').forEach(item => item.removeAttribute('open'))
    setShow(false)
  }
  const reveal = () => {
    setShow(true)
    window.clearTimeout(timer.current)
    timer.current = window.setTimeout(() => { if (!stage.current?.querySelector('details[open]')) setShow(false) }, 3500)
  }
  const feedback = (text: string) => {
    setHint(text)
    window.clearTimeout(hintTimer.current)
    hintTimer.current = window.setTimeout(() => setHint(null), 1000)
  }
  useEffect(() => {
    const element = video.current!
    element.controls = false
    const container = stage.current!
    container.addEventListener('toggle', reveal, true)
    const nativeStart = () => {
      speedHold.finish()
      touch.current = null
      tap.current = null
      window.clearTimeout(clickTimer.current)
      setHint(null)
      setNativeFullscreen(true)
    }
    const nativeEnd = () => { element.controls = false; setNativeFullscreen(false); reveal() }
    element.addEventListener('webkitbeginfullscreen', nativeStart)
    element.addEventListener('webkitendfullscreen', nativeEnd)
    const update = () => {
      if (!activity.current.current && !element.paused) element.pause()
      setState({ paused: element.paused, time: seeking.current ?? restoring.current?.position ?? element.currentTime, duration: restoring.current?.duration || (Number.isFinite(element.duration) ? element.duration : 0), speed: element.playbackRate, fullscreen: document.fullscreenElement === stage.current })
    }
    const pauseHidden = () => { if (document.hidden) { activity.autoplay.current = false; element.pause() } }
    const pause = () => element.pause()
    const onSeeking = () => setBuffering(true)
    const onSeeked = () => { restoring.current = null; seeking.current = null; setTarget(null); setBuffering(false); update() }
    const onReady = () => {
      if (!element.seeking) {
        setBuffering(false)
        if (restoring.current?.position === 0) { restoring.current = null; update() }
      }
    }
    const onError = () => setBuffering(false)
    const onFullscreenChange = () => {
      if (document.fullscreenElement === stage.current && stage.current) lockLandscape(stage.current)
      else { fullscreenRequested.current = false; unlockOrientation() }
      update()
    }
    const events = ['timeupdate', 'durationchange', 'play', 'pause', 'ratechange', 'loadedmetadata']
    events.forEach(event => element.addEventListener(event, update))
    element.addEventListener('seeking', onSeeking)
    element.addEventListener('waiting', onSeeking)
    element.addEventListener('seeked', onSeeked)
    element.addEventListener('canplay', onReady)
    element.addEventListener('playing', onReady)
    element.addEventListener('error', onError)
    element.addEventListener('play', reveal)
    document.addEventListener('fullscreenchange', onFullscreenChange)
    document.addEventListener('visibilitychange', pauseHidden)
    window.addEventListener('pagehide', pause)
    update()
    reveal()
    return () => {
      container.removeEventListener('toggle', reveal, true)
      element.removeEventListener('webkitbeginfullscreen', nativeStart)
      element.removeEventListener('webkitendfullscreen', nativeEnd)
      events.forEach(event => element.removeEventListener(event, update))
      element.removeEventListener('seeking', onSeeking)
      element.removeEventListener('waiting', onSeeking)
      element.removeEventListener('seeked', onSeeked)
      element.removeEventListener('canplay', onReady)
      element.removeEventListener('playing', onReady)
      element.removeEventListener('error', onError)
      element.removeEventListener('play', reveal)
      document.removeEventListener('fullscreenchange', onFullscreenChange)
      fullscreenRequested.current = false
      unlockOrientation()
      document.removeEventListener('visibilitychange', pauseHidden)
      window.removeEventListener('pagehide', pause)
      for (const timerRef of [timer, clickTimer, hintTimer]) window.clearTimeout(timerRef.current)
    }
  }, [video])
  const toggle = () => {
    if (settingsOpen()) return
    const element = video.current!
    if (element.paused) { activity.autoplay.current = true; void element.play().catch(() => undefined) }
    else element.pause()
    reveal()
  }
  const seekTo = (position: number, revealControls = true) => {
    if (settingsOpen()) return
    const element = video.current!
    if (!Number.isFinite(element.duration)) return
    const next = Math.max(0, Math.min(element.duration, position))
    seeking.current = next; setTarget(next); setBuffering(true)
    element.currentTime = next
    if (revealControls) reveal()
  }
  const seek = (offset: number, revealControls = true) => {
    const element = video.current!
    if (Number.isFinite(element.duration)) seekTo((seeking.current ?? element.currentTime) + offset, revealControls)
    feedback(`${offset < 0 ? '−' : '+'}${playbackTime(Math.abs(offset))}`)
  }
  const fullscreen = () => {
    if (document.fullscreenElement) void document.exitFullscreen().catch(() => undefined)
    else if (stage.current?.requestFullscreen) {
      const target = stage.current
      fullscreenRequested.current = true
      const request = target.requestFullscreen()
      // Request rotation in the same user gesture, independently of media readiness.
      lockLandscape(target)
      void request.then(() => {
        fullscreenRequested.current = false
        if (document.fullscreenElement === target) lockLandscape(target)
      }).catch(() => { fullscreenRequested.current = false; unlockOrientation() })
    }
    else (video.current as HTMLVideoElement & { webkitEnterFullscreen?: () => void })?.webkitEnterFullscreen?.()
    reveal()
  }
  const pointerDown = (event: PointerEvent<HTMLDivElement>) => {
    if (settingsOpen()) return
    lastPointer.current = event.pointerType
    suppressClick.current = false
    if (event.pointerType === 'mouse') {
      touch.current = null
      if (event.button !== 0) return
      mouseStart.current = { x: event.clientX, y: event.clientY }
      speedHold.start(`pointer:${event.pointerId}`)
      return
    }
    if (!event.isPrimary) { touch.current = null; tap.current = null; return }
    // Use the visible screen edges, before mapping a rotated fullscreen surface.
    // Ignore the entire contact, even after it moves into the player interior.
    const viewport = window.visualViewport
    const left = viewport?.offsetLeft ?? 0
    const top = viewport?.offsetTop ?? 0
    const width = viewport?.width ?? window.innerWidth
    const height = viewport?.height ?? window.innerHeight
    const edge = 24 / (viewport?.scale || 1)
    if (event.clientX <= left + edge || event.clientX >= left + width - edge || event.clientY <= top + edge || event.clientY >= top + height - edge) {
      cancelGesture()
      return
    }
    const bounds = event.currentTarget.getBoundingClientRect()
    const ratio = rotated() ? (event.clientY - bounds.top) / bounds.height : (event.clientX - bounds.left) / bounds.width
    const zone = ratio < 0.15 ? -1 : ratio > 0.85 ? 1 : 0
    const level = zone < 0 ? 'brightness' : zone > 0 ? 'volume' : null
    event.preventDefault()
    speedHold.start(`pointer:${event.pointerId}`)
    touch.current = { id: event.pointerId, x: event.clientX, y: event.clientY, zone, position: seeking.current ?? video.current!.currentTime, destination: null, axis: null, level, initialLevel: level === 'brightness' ? brightness : (video.current!.muted ? 0 : video.current!.volume) }
    event.currentTarget.setPointerCapture(event.pointerId)
  }
  const pointerMove = (event: PointerEvent<HTMLDivElement>) => {
    if (settingsOpen()) { cancelGesture(); return }
    if (speedHold.isLong()) { event.preventDefault(); return }
    if (event.pointerType === 'mouse') {
      const start = mouseStart.current
      if (start && Math.hypot(event.clientX - start.x, event.clientY - start.y) > 12) speedHold.finish()
      reveal()
      return
    }
    const start = touch.current
    if (!start || start.id !== event.pointerId) return
    const dx = rotated() ? event.clientY - start.y : event.clientX - start.x
    const dy = rotated() ? start.x - event.clientX : event.clientY - start.y
    if (!start.axis && Math.hypot(dx, dy) > 12) {
      speedHold.finish()
      start.axis = Math.abs(dx) > Math.abs(dy) ? 'x' : 'y'
      tap.current = null
      window.clearTimeout(clickTimer.current)
    }
    if (start.axis === 'y') {
      if (!start.level) return
      event.preventDefault()
      const value = Math.max(start.level === 'brightness' ? 0.25 : 0, Math.min(start.level === 'brightness' ? 2 : 1, start.initialLevel - dy / Math.max(1, event.currentTarget.clientHeight) * (start.level === 'brightness' ? 2 : 1)))
      if (start.level === 'brightness') setBrightness(value)
      else changeVolume(value)
      feedback(`${start.level === 'brightness' ? t('亮度') : t('音量')} ${Math.round(value * 100)}%`)
      return
    }
    if (start.axis !== 'x') return
    event.preventDefault()
    const duration = video.current!.duration
    if (!Number.isFinite(duration) || duration <= 0) return
    const delta = dx / Math.max(1, event.currentTarget.clientWidth) * Math.min(duration, 120)
    start.destination = Math.max(0, Math.min(duration, start.position + delta))
    setTarget(start.destination)
    feedback(playbackTime(start.destination))
  }
  const cancelGesture = () => {
    speedHold.finish()
    mouseStart.current = null
    touch.current = null
    tap.current = null
    setTarget(null)
    setHint(null)
    window.clearTimeout(clickTimer.current)
  }
  const pointerUp = (event: PointerEvent<HTMLDivElement>) => {
    mouseStart.current = null
    if (speedHold.finish(`pointer:${event.pointerId}`)) {
      suppressClick.current = true
      touch.current = null
      tap.current = null
      window.clearTimeout(clickTimer.current)
      event.preventDefault()
      return
    }
    if (settingsOpen()) { cancelGesture(); return }
    const start = touch.current
    if (event.pointerType === 'mouse' || !start || start.id !== event.pointerId) return
    touch.current = null
    if (start.axis) {
      if (start.destination !== null) seekTo(start.destination, false)
      return
    }
    event.preventDefault()
    const previous = tap.current
    const now = performance.now()
    if (previous && now - previous.time < 320 && previous.zone === start.zone) {
      tap.current = null
      window.clearTimeout(clickTimer.current)
      if (start.zone) seek(start.zone * 30, false)
      else toggle()
    } else {
      tap.current = { time: now, zone: start.zone }
      window.clearTimeout(clickTimer.current)
      if (show) hide()
      clickTimer.current = window.setTimeout(() => { tap.current = null; if (!show) reveal() }, 320)
    }
  }
  return <div ref={stage} className={`video-stage${show ? ' controls-visible' : ''}`} tabIndex={0} aria-label={t('应用内播放器')}
    onPointerDownCapture={event => {
      speedHold.finish()
      if (event.pointerType === 'mouse' && event.button === 0 && !(event.target as HTMLElement).closest(PLAYER_INTERACTIVE)) stage.current?.focus({ preventScroll: true })
    }}
    onPointerMove={event => { if (event.pointerType === 'mouse') reveal() }}
    onPointerLeave={event => { if (event.pointerType === 'mouse') hide() }}
    onFocus={event => { if ((event.target as HTMLElement).matches(':focus-visible')) reveal() }}
    onBlur={() => speedHold.finish()}
    onKeyDown={event => {
      if (!activity.active || nativeFullscreen || event.defaultPrevented || event.nativeEvent.isComposing || event.ctrlKey || event.metaKey || event.altKey || event.shiftKey) return
      const target = event.target as HTMLElement
      if (target.closest(`${PLAYER_EDITABLE}, [role="slider"]`) || settingsOpen()) return
      const key = event.key.toLowerCase()
      // Space still activates the focused native button or summary exactly once.
      if (key === ' ' && target.closest(PLAYER_INTERACTIVE)) return
      if (key === 'arrowright') { event.preventDefault(); if (!event.repeat) speedHold.start('keyboard', () => seek(5)); return }
      if (key === 'arrowleft') { event.preventDefault(); seek(-5); return }
      if (key === ' ' || key === 'k') { event.preventDefault(); if (!event.repeat) toggle() }
      if (key === 'f') { event.preventDefault(); if (!event.repeat) fullscreen() }
    }}>
    <div className="video-surface">{children}
    {!nativeFullscreen ? <>
    <button type="button" className="video-exit" aria-label={state.fullscreen ? t('退出全屏') : t('返回')} onClick={() => { if (document.fullscreenElement) fullscreen(); else activity.exit?.() }}><ArrowLeft aria-hidden="true" /></button>
    {buffering ? <div className="video-buffering" role="status" aria-label={t('正在加载')}><span className="video-buffering-spinner" /></div> : null}
    <div className="video-gesture-surface" aria-hidden="true" onPointerDown={pointerDown} onPointerMove={pointerMove} onPointerUp={pointerUp} onPointerCancel={cancelGesture} onLostPointerCapture={() => { if (touch.current) cancelGesture() }}
      onClick={() => { if (suppressClick.current) { suppressClick.current = false; return }; if (lastPointer.current !== 'mouse') return; window.clearTimeout(clickTimer.current); clickTimer.current = window.setTimeout(toggle, 250) }}
      onDoubleClick={() => { if (lastPointer.current !== 'mouse') return; window.clearTimeout(clickTimer.current); fullscreen() }} />
    {speedHold.holding || hint ? <div className="video-gesture-hint" role="status">{speedHold.holding ? '2×' : hint}</div> : null}
    <div className="video-controls" onPointerDown={reveal} onFocus={event => { if ((event.target as HTMLElement).matches(':focus-visible')) reveal() }}>
      <div className="video-progress-row"><span>{playbackTime(target ?? state.time)}</span>
        <input aria-label={t('播放进度')} type="range" min={0} max={state.duration || 1} step={0.1} value={Math.min(target ?? state.time, state.duration || 1)} disabled={!state.duration}
          onChange={event => seekTo(Number(event.target.value))} />
        <span>{playbackTime(state.duration)}</span></div>
      <div className="video-controls-row">
        <button type="button" aria-label={state.paused ? t('播放') : t('暂停')} onClick={toggle}>{state.paused ? <Play /> : <Pause />}</button>
        <button type="button" aria-label={t('后退 30 秒')} onClick={() => seek(-30)}><RotateCcw /></button>
        <button type="button" aria-label={t('前进 30 秒')} onClick={() => seek(30)}><RotateCw /></button>
        <div className="video-primary-options"><div className="player-level-menu"><PlayerMenu label={t('亮度与音量')} value={<SlidersHorizontal aria-hidden="true" />}><div className="player-levels">
          <label><Sun aria-hidden="true" /><input type="range" aria-label={t('亮度')} min={25} max={200} value={Math.round(brightness * 100)} onChange={event => setBrightness(Number(event.target.value) / 100)} /><output>{Math.round(brightness * 100)}%</output></label>
          <label><Volume2 aria-hidden="true" /><input type="range" aria-label={t('音量')} min={0} max={100} value={Math.round(volume * 100)} onChange={event => changeVolume(Number(event.target.value) / 100)} /><output>{Math.round(volume * 100)}%</output></label>
        </div></PlayerMenu></div><PlayerSubtitles video={video} sources={subtitles} />{resolution}<PlayerChoice label={t('播放速度')} value={state.speed} onChange={speed => { video.current!.playbackRate = speed }} options={[0.5, 0.75, 1, 1.25, 1.5, 2].map(speed => ({ value: speed, label: `${speed}×` }))} /></div>
        <button type="button" aria-label={state.fullscreen ? t('退出全屏') : t('全屏')} onClick={fullscreen}>{state.fullscreen ? <Minimize /> : <Expand />}</button>
      </div>
    </div>
    </> : null}</div>
  </div>
}
