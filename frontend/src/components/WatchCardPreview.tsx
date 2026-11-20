import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'
import { watchCoverUrl, type WatchEntry } from '../lib/watching'
import { StableImage } from '../pages/WorkUi'

function PreviewClip({ source, url }: { source: string; url: string }) {
  const [clip, setClip] = useState<{ url: string; gif: boolean } | null>(null)
  const [show, setShow] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    let objectUrl = ''
    async function load() {
      const response = await fetch(`/api/watch/preview?${new URLSearchParams({ source, url })}`, { signal: controller.signal })
      if (!response.ok) return
      const blob = await response.blob()
      if (controller.signal.aborted) return
      objectUrl = URL.createObjectURL(blob)
      setClip({ url: objectUrl, gif: blob.type === 'image/gif' })
    }
    void load().catch(() => {})
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [source, url])
  if (!clip) return null
  const className = `watch-preview-clip${show ? ' is-playing' : ''}`
  return clip.gif
    ? <img src={clip.url} alt="" className={className} onLoad={() => setShow(true)} onError={() => setShow(false)} />
    : <video src={clip.url} className={className} muted autoPlay loop playsInline disableRemotePlayback
        onPlaying={() => setShow(true)} onError={() => setShow(false)} />
}

export function WatchCardPreview({ entry, href, children }: { entry: WatchEntry; href: string; children: ReactNode }) {
  const [active, setActive] = useState(false)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const touch = useRef<{ x: number; y: number } | null>(null)
  const held = useRef(false)
  const card = useRef<HTMLElement>(null)
  const stop = () => {
    clearTimeout(timer.current)
    touch.current = null
    setActive(false)
  }
  const schedule = (isTouch: boolean) => {
    clearTimeout(timer.current)
    if (!entry.preview) return
    timer.current = setTimeout(() => {
      held.current = isTouch
      setActive(true)
    }, isTouch ? 350 : 180)
  }
  useEffect(() => () => clearTimeout(timer.current), [])
  useEffect(() => {
    if (!active || !card.current) return
    const observer = new IntersectionObserver(([item]) => { if (!item.isIntersecting) stop() })
    observer.observe(card.current)
    const hidden = () => { if (document.hidden) stop() }
    document.addEventListener('visibilitychange', hidden)
    return () => { observer.disconnect(); document.removeEventListener('visibilitychange', hidden) }
  }, [active])
  return <article ref={card} className="watch-card"
    onPointerEnter={event => { if (event.pointerType === 'mouse') schedule(false) }}
    onPointerLeave={event => { if (event.pointerType === 'mouse') stop() }}>
    <Link className="watch-card-cover" to={href} tabIndex={-1} aria-hidden="true" draggable={false}
      onPointerDown={event => {
        held.current = false
        if (event.pointerType === 'mouse' || !event.isPrimary || !entry.preview) return
        touch.current = { x: event.clientX, y: event.clientY }
        schedule(true)
      }}
      onPointerMove={event => {
        const start = touch.current
        if (start && Math.hypot(event.clientX - start.x, event.clientY - start.y) > 10) stop()
      }}
      onPointerUp={event => { if (event.pointerType !== 'mouse') stop() }}
      onPointerCancel={stop}
      onContextMenu={event => { if (touch.current || held.current) event.preventDefault() }}
      onClick={event => { if (held.current) { event.preventDefault(); held.current = false } }}>
      <StableImage src={watchCoverUrl(entry.source, entry.cover)} alt="" loading="lazy" referrerPolicy="no-referrer" draggable={false} />
      {active && entry.preview ? <PreviewClip key={entry.preview} source={entry.source} url={entry.preview} /> : null}
    </Link>
    {children}
  </article>
}
