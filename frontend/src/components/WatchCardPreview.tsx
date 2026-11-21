import { useEffect, useRef, useState, type ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { t } from '../lib/i18n'
import { coverImageUrl } from '../lib/api'
import { type WatchEntry } from '../lib/watching'
import { StableImage } from '../pages/WorkUi'

function PreviewClip({ source, url, touch }: { source: string; url: string; touch: boolean }) {
  const [clip, setClip] = useState<{ url: string; gif: boolean } | null>(null)
  const [show, setShow] = useState(false)
  const [failed, setFailed] = useState(false)
  useEffect(() => {
    const controller = new AbortController()
    let objectUrl = ''
    async function load() {
      const response = await fetch(`/api/watch/preview?${new URLSearchParams({ source, url })}`, { signal: controller.signal })
      if (!response.ok) throw new Error('Preview unavailable')
      const blob = await response.blob()
      if (controller.signal.aborted) return
      objectUrl = URL.createObjectURL(blob)
      setClip({ url: objectUrl, gif: blob.type === 'image/gif' })
    }
    void load().catch(() => { if (!controller.signal.aborted) setFailed(true) })
    return () => {
      controller.abort()
      if (objectUrl) URL.revokeObjectURL(objectUrl)
    }
  }, [source, url])
  const className = `watch-preview-clip${show ? ' is-playing' : ''}`
  return <>
    {clip && !failed && (clip.gif
    ? <img src={clip.url} alt="" className={className} onLoad={() => setShow(true)} onError={() => setFailed(true)} />
    : <video src={clip.url} className={className} muted autoPlay loop playsInline disableRemotePlayback
        onPlaying={() => setShow(true)} onError={() => setFailed(true)} />)}
    {touch && <span className="watch-preview-status">{failed ? t('加载失败') : show ? t('播放') : t('正在加载')}</span>}
  </>
}

export function WatchCardPreview({ entry, href, playHref, children }: { entry: WatchEntry; href: string; playHref: string; children: ReactNode }) {
  const navigate = useNavigate()
  const [active, setActive] = useState<'mouse' | 'touch' | null>(null)
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined)
  const touch = useRef(false)
  const card = useRef<HTMLElement>(null)
  const stop = () => {
    clearTimeout(timer.current)
    setActive(null)
  }
  useEffect(() => () => clearTimeout(timer.current), [])
  useEffect(() => {
    if (!active || !card.current) return
    const observer = new IntersectionObserver(([item]) => { if (!item.isIntersecting) stop() })
    observer.observe(card.current)
    const hidden = () => { if (document.hidden) stop() }
    const outside = (event: PointerEvent) => {
      if (event.target instanceof Node && !card.current?.contains(event.target)) stop()
    }
    document.addEventListener('pointerdown', outside, true)
    document.addEventListener('visibilitychange', hidden)
    return () => {
      observer.disconnect()
      document.removeEventListener('visibilitychange', hidden)
      document.removeEventListener('pointerdown', outside, true)
    }
  }, [active])
  return <article ref={card} className="watch-card"
    onPointerEnter={event => {
      if (event.pointerType === 'mouse' && entry.preview) {
        clearTimeout(timer.current)
        timer.current = setTimeout(() => setActive('mouse'), 180)
      }
    }}
    onPointerLeave={event => { if (event.pointerType === 'mouse') stop() }}>
    <Link className="watch-card-cover" to={href} tabIndex={-1} aria-hidden="true" draggable={false}
      onPointerDown={event => {
        touch.current = event.isPrimary && (event.pointerType === 'touch' || event.pointerType === 'pen')
      }}
      onPointerCancel={() => { touch.current = false }}
      onClick={event => {
        if (!touch.current || event.detail === 0) return
        touch.current = false
        event.preventDefault()
        if (entry.preview && active !== 'touch') setActive('touch')
        else navigate(playHref)
      }}>
      <StableImage src={coverImageUrl(entry.source, entry.cover)} alt="" loading="lazy" referrerPolicy="no-referrer" draggable={false} />
      {active && entry.preview ? <PreviewClip key={entry.preview} source={entry.source} url={entry.preview} touch={active === 'touch'} /> : null}
    </Link>
    {children}
  </article>
}
