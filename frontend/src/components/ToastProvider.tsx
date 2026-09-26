import { AlertCircle, CheckCircle2, Info, X } from 'lucide-react'
import { createContext, type ReactNode, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'

import { IconButton } from './ui'

type ToastTone = 'success' | 'error' | 'info'
interface ToastItem {
  id: number
  message: string
  tone: ToastTone
}

const ToastContext = createContext<{ push: (message: string, tone?: ToastTone) => void } | null>(null)

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([])
  const nextId = useRef(1)
  const timers = useRef(new Map<number, number>())
  const dismiss = useCallback((id: number) => {
    const timer = timers.current.get(id)
    if (timer !== undefined) window.clearTimeout(timer)
    timers.current.delete(id)
    setItems((current) => current.filter((item) => item.id !== id))
  }, [])
  const push = useCallback((message: string, tone: ToastTone = 'info') => {
    const id = nextId.current++
    setItems((current) => [...current.slice(-2), { id, message, tone }])
    timers.current.set(id, window.setTimeout(() => dismiss(id), 4500))
  }, [dismiss])
  useEffect(() => () => {
    for (const timer of timers.current.values()) window.clearTimeout(timer)
    timers.current.clear()
  }, [])
  const value = useMemo(() => ({ push }), [push])

  return (
    <ToastContext.Provider value={value}>
      {children}
      <div className="toast-viewport" aria-live="polite" aria-atomic="false">
        {items.map((item) => {
          const Icon = item.tone === 'success' ? CheckCircle2 : item.tone === 'error' ? AlertCircle : Info
          return (
            <div className={`toast toast-${item.tone}`} role={item.tone === 'error' ? 'alert' : 'status'} key={item.id}>
              <Icon aria-hidden="true" />
              <span>{item.message}</span>
              <IconButton label="关闭通知" size="small" onClick={() => dismiss(item.id)}>
                <X aria-hidden="true" />
              </IconButton>
            </div>
          )
        })}
      </div>
    </ToastContext.Provider>
  )
}

export function useToast() {
  const context = useContext(ToastContext)
  if (!context) throw new Error('useToast must be used inside ToastProvider')
  return context
}
