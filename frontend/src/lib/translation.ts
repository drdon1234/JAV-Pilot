import { useQuery } from '@tanstack/react-query'
import { useCallback, useEffect, useRef, useState } from 'react'

import { api } from './api'
import { translationTarget } from './i18n'
import type { WorkflowTranslationDefaults } from '../types'

export interface TranslationPreferences {
  enabled: boolean
  showOriginal: boolean
}

const STORAGE_KEY = 'jav-pilot:translation'
const CHANGE_EVENT = 'jav-pilot:translation-change'

function read(defaults: TranslationPreferences): TranslationPreferences {
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY)
    if (!raw) return defaults
    const value = JSON.parse(raw) as Partial<TranslationPreferences>
    return {
      enabled: typeof value.enabled === 'boolean' ? value.enabled : defaults.enabled,
      showOriginal: typeof value.showOriginal === 'boolean' ? value.showOriginal : defaults.showOriginal,
    }
  } catch {
    return defaults
  }
}

/**
 * One translation switch shared by the search options, the results toolbar
 * and the detail page. A change anywhere applies everywhere immediately.
 */
export function useTranslationPreferences(configured?: WorkflowTranslationDefaults | null) {
  const defaults: TranslationPreferences = {
    enabled: configured?.enabled ?? false,
    showOriginal: configured?.show_original ?? false,
  }
  const [preferences, setPreferences] = useState(() => read(defaults))
  const latest = useRef(preferences)
  latest.current = preferences

  useEffect(() => {
    setPreferences(read(defaults))
    const sync = (event: Event) => {
      // Same-page changes carry the value, so the switch also works when
      // storage is unavailable; other tabs report through the storage event.
      setPreferences(event instanceof CustomEvent && event.detail ? event.detail as TranslationPreferences : read(defaults))
    }
    globalThis.addEventListener?.(CHANGE_EVENT, sync)
    globalThis.addEventListener?.('storage', sync)
    return () => {
      globalThis.removeEventListener?.(CHANGE_EVENT, sync)
      globalThis.removeEventListener?.('storage', sync)
    }
  }, [defaults.enabled, defaults.showOriginal])

  const update = useCallback((next: Partial<TranslationPreferences>) => {
    const merged = { ...latest.current, ...next }
    latest.current = merged
    setPreferences(merged)
    try {
      globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(merged))
    } catch {
      // Keeps working for this page when storage is unavailable.
    }
    globalThis.dispatchEvent?.(new CustomEvent(CHANGE_EVENT, { detail: merged }))
  }, [])

  return { preferences, update }
}

/** Translations for the given texts, keyed by the original text. */
export function useTranslations(texts: readonly string[], enabled: boolean) {
  const unique = Array.from(new Set(texts.map((text) => text.trim()).filter(Boolean))).slice(0, 60)
  const query = useQuery({
    queryKey: ['translations', translationTarget(), unique],
    queryFn: async () => {
      const payload = await api.translate(unique)
      return new Map(unique.map((text, index) => [text, payload.translations[index] ?? null]))
    },
    enabled: enabled && unique.length > 0,
    staleTime: Infinity,
    gcTime: 60 * 60_000,
    retry: 1,
  })
  return {
    translate: (text: string | null | undefined) => (text ? query.data?.get(text.trim()) ?? null : null),
    loading: query.isFetching,
    error: query.isError,
  }
}
