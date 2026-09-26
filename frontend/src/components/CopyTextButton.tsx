import { Clipboard, X } from 'lucide-react'
import { useEffect, useId, useRef, useState } from 'react'

import { useToast } from './ToastProvider'
import { Button, IconButton } from './ui'

export function CopyTextButton({ text, label }: { text: string; label: string }) {
  const toast = useToast()
  const [manual, setManual] = useState(false)
  const [copying, setCopying] = useState(false)
  const input = useRef<HTMLTextAreaElement>(null)
  const trigger = useRef<HTMLSpanElement>(null)
  const hintId = useId()

  useEffect(() => {
    if (manual) {
      input.current?.focus()
      input.current?.select()
    }
  }, [manual])

  function close() {
    setManual(false)
    trigger.current?.querySelector('button')?.focus()
  }

  async function copy() {
    if (!navigator.clipboard?.writeText) {
      setManual(true)
      return
    }
    setCopying(true)
    try {
      await navigator.clipboard.writeText(text)
      toast.push('磁链已复制', 'success')
    } catch {
      setManual(true)
    } finally {
      setCopying(false)
    }
  }

  return (
    <span className="copy-text-control" ref={trigger}>
      <IconButton type="button" label={label} size="small" disabled={!text || copying} aria-expanded={manual} onClick={() => void copy()}>
        <Clipboard aria-hidden="true" />
      </IconButton>
      {manual ? (
        <span className="manual-copy" onKeyDown={(event) => {
          if (event.key === 'Escape') { event.preventDefault(); close() }
        }}>
          <span id={hintId}>请选择并复制下方磁链；电脑可按 Ctrl+C，手机可长按复制。</span>
          <textarea ref={input} readOnly value={text} aria-label="待复制的磁链" aria-describedby={hintId} rows={3} onFocus={(event) => event.target.select()} />
          <span className="manual-copy-actions">
            <Button type="button" size="small" onClick={() => { input.current?.focus(); input.current?.select() }}>全选磁链</Button>
            <Button type="button" size="small" variant="ghost" onClick={close}><X aria-hidden="true" />关闭</Button>
          </span>
        </span>
      ) : null}
    </span>
  )
}
