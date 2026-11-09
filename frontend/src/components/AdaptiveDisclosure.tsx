import { useEffect, useState, type ReactNode } from 'react'
import { ChevronDown } from 'lucide-react'

/** Keep secondary tools available without placing their forms above mobile content. */
export function AdaptiveDisclosure({ title, children, id, className = '' }: { title: ReactNode; children: ReactNode; id?: string; className?: string }) {
  const [open, setOpen] = useState(() => !globalThis.matchMedia?.('(max-width: 720px)').matches || Boolean(id && globalThis.location?.hash === `#${id}`))
  useEffect(() => {
    const media = globalThis.matchMedia('(max-width: 720px)')
    const resize = () => setOpen(!media.matches)
    const anchor = () => { if (id && location.hash === `#${id}`) setOpen(true) }
    media.addEventListener('change', resize)
    window.addEventListener('hashchange', anchor)
    return () => { media.removeEventListener('change', resize); window.removeEventListener('hashchange', anchor) }
  }, [id])
  return <details id={id} className={`adaptive-disclosure ${className}`} open={open} onToggle={event => setOpen(event.currentTarget.open)} onInvalidCapture={event => {
    event.currentTarget.open = true
    setOpen(true)
  }}>
    <summary>{title}<ChevronDown aria-hidden="true" /></summary>
    <div className="adaptive-disclosure-content">{children}</div>
  </details>
}
