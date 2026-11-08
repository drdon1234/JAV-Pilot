import { useQueries } from '@tanstack/react-query'

import type { RankingOption } from '../types'
import { api } from './api'
import { currentLocale, type Locale } from './i18n'
import labels from './rankingCategoryLabels.json'
import { serverText } from './serverTexts'

// Reviewed HentaiPark vocabulary; attribution: docs/licenses/hentaipark-tags.txt.
// Display-only terminology. Keep source labels and category IDs intact.
// The FANZA genre vocabulary is scoped to its board, never to titles or names.
const normalize = (label: string) => label.normalize('NFKC').trim().toLowerCase()
const fanzaLabels = new Map(Object.entries(labels).map(([label, translations]) => [normalize(label), translations]))

export function knownRankingCategory(board: string, label: string, locale: Locale): string | undefined {
  if (board !== 'fanza_genre' || (locale !== 'zh-CN' && locale !== 'zh-TW')) return undefined
  return fanzaLabels.get(normalize(label))?.[locale]
}

/** Translate source-owned category options, independently of the title toggle. */
export function useRankingCategories(board: string, options: readonly RankingOption[], dynamic: boolean) {
  const locale = currentLocale()
  // Chinese categories use reviewed terminology only. Unknown source labels
  // stay intact rather than acquiring misleading dictionary translations.
  const pending = dynamic && locale !== 'ja' && locale !== 'zh-CN' && locale !== 'zh-TW'
    ? Array.from(new Set(options.map((option) => option.label.trim()).filter((label) => (
      label && !knownRankingCategory(board, label, locale)
    ))))
    : []
  // The translation endpoint accepts at most 60 texts. Do not silently drop
  // later categories as the title translation hook's visible-page cap does.
  const chunks: string[][] = []
  for (let start = 0; start < pending.length; start += 60) chunks.push(pending.slice(start, start + 60))
  const queries = useQueries({
    queries: chunks.map((texts) => ({
      queryKey: ['ranking-category-translations', board, locale, texts],
      queryFn: async () => {
        const result = await api.translate(texts, locale)
        return new Map(texts.map((text, index) => [text, result.translations[index]?.trim() || null]))
      },
      staleTime: Infinity,
      gcTime: 60 * 60_000,
      retry: 1,
    })),
  })
  const translated = new Map(queries.flatMap((query) => Array.from(query.data ?? [])))
  return options.map((option) => ({
    ...option,
    originalLabel: option.label,
    label: knownRankingCategory(board, option.label, locale)
      ?? translated.get(option.label.trim())
      ?? (dynamic ? option.label : serverText(option.label)),
  }))
}
