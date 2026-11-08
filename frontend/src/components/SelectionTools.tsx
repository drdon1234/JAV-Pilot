import { useEffect, useState, type ReactNode } from 'react'
import { ListChecks } from 'lucide-react'
import { Button } from './ui'
import { t } from '../lib/i18n'

export function useSelection(scope = '') {
  const [managing, setManaging] = useState(false)
  const [selected, setSelected] = useState<Set<string>>(new Set())
  useEffect(() => { setSelected(new Set()); setManaging(false) }, [scope])
  return { managing, selected, clear: () => setSelected(new Set()), finish: () => { setManaging(false); setSelected(new Set()) }, start: () => setManaging(true),
    toggle: (id: string) => setSelected(previous => { const next = new Set(previous); if (next.has(id)) next.delete(id); else next.add(id); return next }),
    all: (ids: string[]) => setSelected(new Set(ids)),
  }
}
type Selection = ReturnType<typeof useSelection>

export function SelectionTools({ selection, ids, children, busy = false }: { selection: Selection; ids: string[]; children?: ReactNode; busy?: boolean }) {
  if (!ids.length) return null
  return <div className="selection-tools" aria-label={t('批量管理')}>
    {!selection.managing ? <Button size="small" variant="ghost" onClick={selection.start}><ListChecks aria-hidden="true" />{t('批量管理')}</Button> : <>
      <label className="selection-all"><input type="checkbox" disabled={busy} checked={ids.length > 0 && ids.every(id => selection.selected.has(id))} onChange={event => event.target.checked ? selection.all(ids) : selection.clear()} />{t('全选已加载')}</label>
      <span role="status">{t('已选 {count} 项', { count: selection.selected.size })}</span>
      <div className="selection-actions">{children}</div>
      <Button size="small" variant="ghost" onClick={selection.finish} disabled={busy}>{t('完成')}</Button>
    </>}
  </div>
}

export function SelectionCheck({ selection, id, name, disabled = false }: { selection: Selection; id: string; name: string; disabled?: boolean }) {
  return selection.managing ? <label className="selection-check"><input type="checkbox" checked={selection.selected.has(id)} disabled={disabled} onChange={() => selection.toggle(id)} /><span className="sr-only">{t('选择 {name}', { name })}</span></label> : null
}

export function RemoveSelection({ count, busy, onRemove }: { count: number; busy: boolean; onRemove: () => void }) {
  const [confirm, setConfirm] = useState(false)
  useEffect(() => { setConfirm(false) }, [count])
  return confirm ? <><span>{t('确认移除所选项目？')}</span><Button size="small" variant="danger" disabled={busy || !count} onClick={() => { setConfirm(false); onRemove() }}>{t('确认移除')}</Button><Button size="small" variant="ghost" onClick={() => setConfirm(false)} disabled={busy}>{t('取消')}</Button></> : <Button size="small" variant="ghost" disabled={busy || !count} onClick={() => setConfirm(true)}>{t('移除所选')}</Button>
}
