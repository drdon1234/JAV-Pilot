import { useQuery } from '@tanstack/react-query'
import { useCallback, useState, useSyncExternalStore } from 'react'

import { api } from './api'
import { serviceErrorMessage } from './presentation'

const MAX_TEXTS_PER_REQUEST = 60
const MAX_REMEMBERED = 2000

/**
 * AI translations obtained in this tab, shared by the results list, the
 * detail page and the rankings. They only ever come from a button press;
 * nothing here requests an AI translation on its own.
 */
const store = {
  translations: new Map<string, string>(),
  hidden: false,
  version: 0,
}
const listeners = new Set<() => void>()

function emit() {
  store.version += 1
  listeners.forEach((listener) => listener())
}

function subscribe(listener: () => void) {
  listeners.add(listener)
  return () => {
    listeners.delete(listener)
  }
}

function remember(text: string, translation: string) {
  store.translations.delete(text)
  store.translations.set(text, translation)
  while (store.translations.size > MAX_REMEMBERED) {
    const oldest = store.translations.keys().next().value
    if (oldest === undefined) break
    store.translations.delete(oldest)
  }
}

export function useAiTranslationConfig() {
  return useQuery({
    queryKey: ['ai-translation-config'],
    queryFn: api.aiTranslationConfig,
    staleTime: 60_000,
  })
}

export interface AiTranslationMessage {
  tone: 'error' | 'info'
  text: string
}

export function useAiTranslations(texts: readonly string[]) {
  useSyncExternalStore(subscribe, () => store.version)
  const [running, setRunning] = useState(false)
  const [message, setMessage] = useState<AiTranslationMessage | null>(null)
  const unique = Array.from(new Set(texts.map((text) => text.trim()).filter(Boolean)))
  const missing = unique.filter((text) => !store.translations.has(text))
  const missingKey = missing.join('\u0000')

  const run = useCallback(async () => {
    const pending = missingKey ? missingKey.split('\u0000') : []
    if (!pending.length) return
    setRunning(true)
    setMessage(null)
    let untranslated = 0
    let refused = 0
    let reason: string | null = null
    try {
      for (let start = 0; start < pending.length; start += MAX_TEXTS_PER_REQUEST) {
        const chunk = pending.slice(start, start + MAX_TEXTS_PER_REQUEST)
        const payload = await api.aiTranslate(chunk)
        chunk.forEach((text, index) => {
          const value = payload.translations[index]
          if (value) remember(text, value)
          else untranslated += 1
        })
        refused += payload.refused
        reason = reason ?? payload.error
        store.hidden = false
        emit()
      }
      if (untranslated) {
        setMessage({
          tone: 'error',
          text: refused >= untranslated
            ? `${untranslated} 条被模型拒绝翻译，可换用其他模型`
            : `${untranslated} 条未能翻译${reason ? `：${reason}` : ''}`,
        })
      }
    } catch (error) {
      setMessage({ tone: 'error', text: serviceErrorMessage(error, 'AI 翻译失败，请稍后重试') })
    } finally {
      setRunning(false)
    }
  }, [missingKey])

  const setHidden = useCallback((hidden: boolean) => {
    store.hidden = hidden
    emit()
  }, [])

  return {
    translate: (text: string | null | undefined) => (
      text && !store.hidden ? store.translations.get(text.trim()) ?? null : null
    ),
    hidden: store.hidden,
    total: unique.length,
    missing: missing.length,
    running,
    message,
    clearMessage: () => setMessage(null),
    run,
    setHidden,
  }
}

export type AiTranslationState = ReturnType<typeof useAiTranslations>
