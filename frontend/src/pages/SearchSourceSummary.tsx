import type { SiteSettings, WorkResult } from '../types'
import { sourceName } from './WorkUi'

interface SourceSummaryItem {
  id: string
  name: string
  count: number
  note: string
  state: 'ok' | 'skipped' | 'unavailable' | 'pending'
}

export function summarizeSources(
  sourceIds: readonly string[],
  sites: readonly SiteSettings[],
  results: readonly WorkResult[],
  skipped: Readonly<Record<string, string>>,
  errors: Readonly<Record<string, string>>,
  busy: boolean,
): SourceSummaryItem[] {
  return sourceIds.map((id) => {
    const count = results.filter((result) => result.sources.some((source) => source.source_id === id)).length
    const name = sourceName(id, sites)
    if (skipped[id]) return { id, name, count, note: skipped[id], state: 'skipped' }
    if (errors[id]) return { id, name, count, note: count ? '部分页面暂时无法访问' : '暂时无法访问', state: 'unavailable' }
    if (busy && !count) return { id, name, count, note: '搜索中', state: 'pending' }
    return { id, name, count, note: '', state: 'ok' }
  })
}

/**
 * One neutral line explaining where every selected source's results went,
 * including sources that could not take part in this query.
 */
export function SearchSourceSummary({ items }: { items: SourceSummaryItem[] }) {
  if (!items.length) return null
  return (
    <ul className="search-source-summary" aria-label="各来源结果">
      {items.map((item) => (
        <li key={item.id} className={`source-summary-${item.state}`}>
          <span>{item.name}</span>
          {item.state === 'ok' || item.count ? <strong>{item.count}</strong> : null}
          {item.note ? <small>{item.note}</small> : null}
        </li>
      ))}
    </ul>
  )
}
