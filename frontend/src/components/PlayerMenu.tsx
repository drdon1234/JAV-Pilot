import { useEffect, useId, useRef, type ReactNode } from 'react'
import { Check, X } from 'lucide-react'
import { t } from '../lib/i18n'

export function PlayerMenu({ label, value, children, wide = false }: { label: string; value: ReactNode; children: ReactNode; wide?: boolean }) {
  const root = useRef<HTMLDetailsElement>(null)
  const id = useId()
  useEffect(() => {
    const close = (event: PointerEvent) => { if (!root.current?.contains(event.target as Node)) root.current?.removeAttribute('open') }
    document.addEventListener('pointerdown', close)
    return () => document.removeEventListener('pointerdown', close)
  }, [])
  return <details ref={root} className={`player-menu${wide ? ' player-menu-wide' : ''}`} onToggle={event => {
    if (!event.currentTarget.open) return
    event.currentTarget.closest('.video-stage')?.querySelectorAll('details[open]').forEach(item => { if (item !== event.currentTarget) item.removeAttribute('open') })
  }} onBlur={event => { if (event.relatedTarget && !event.currentTarget.contains(event.relatedTarget as Node)) root.current?.removeAttribute('open') }} onKeyDown={event => {
    if (event.key === 'Escape') { event.stopPropagation(); root.current?.removeAttribute('open'); root.current?.querySelector('summary')?.focus() }
    if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
      if ((event.target as HTMLElement).closest('input')) return
      const buttons = Array.from(root.current?.querySelectorAll<HTMLButtonElement>('[role=menuitemradio]') ?? [])
      if (!buttons.length) return
      event.preventDefault()
      const index = buttons.indexOf(document.activeElement as HTMLButtonElement)
      buttons[(index + (event.key === 'ArrowDown' ? 1 : -1) + buttons.length) % buttons.length]?.focus()
    }
  }}>
    <summary aria-label={label} aria-controls={id}>{value}</summary>
    <div id={id} className="player-menu-panel" aria-label={label}>{wide ? <div className="player-menu-heading"><strong>{label}</strong><button type="button" aria-label={t('关闭')} onClick={() => { root.current?.removeAttribute('open'); root.current?.querySelector('summary')?.focus() }}><X aria-hidden="true" /></button></div> : null}{children}</div>
  </details>
}

export function PlayerChoice({ label, value, options, onChange }: { label: string; value: number; options: { value: number; label: string }[]; onChange: (value: number) => void }) {
  return <PlayerMenu label={label} value={options.find(option => option.value === value)?.label ?? label}>
    <div role="menu" aria-label={label}>{options.map(option => <button key={option.value} type="button" role="menuitemradio" aria-checked={option.value === value} onClick={event => {
      onChange(option.value)
      const root = event.currentTarget.closest('details')
      root?.removeAttribute('open'); root?.querySelector('summary')?.focus()
    }}><span>{option.label}</span>{option.value === value ? <Check aria-hidden="true" /> : null}</button>)}</div>
  </PlayerMenu>
}
