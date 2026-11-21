import { useRef } from 'react'
import { useQuery, useQueryClient } from '@tanstack/react-query'
import { requestJson } from '../lib/api'
import { t } from '../lib/i18n'
import { createWebDownloadIntentKey } from '../lib/webDownloads'

type Action = 'download' | 'subtitles' | 'metadata'
type Batch = { id: string; action: Action; total: number; done: number; failed: number; skipped: number }
type Selection =
  | { action: 'download'; items: { code: string; variant: string }[] }
  | { action: 'subtitles' | 'metadata'; grouped: boolean; items: string[]; filters: object }

export function useSubmitSelection(scope: string) {
  const pending = useRef({ body: '', id: '' })
  const client = useQueryClient()
  return async (selection: Selection) => {
    const body = JSON.stringify({ scope, ...selection })
    if (pending.current.body !== body) pending.current = { body, id: createWebDownloadIntentKey() }
    const result = await requestJson<{ id: string }>('/api/selection-batches', {
      method: 'POST', body: JSON.stringify({ scope, ...selection, id: pending.current.id }), timeoutMs: 120_000,
    })
    pending.current = { body: '', id: '' }
    void client.invalidateQueries({ queryKey: ['selection-batches', scope] })
    return result.id
  }
}

export function SelectionBatches({ scope }: { scope: string }) {
  const recent = useQuery({
    queryKey: ['selection-batches', scope],
    queryFn: ({ signal }) => requestJson<{ batches: Batch[] }>(
      `/api/selection-batches?${new URLSearchParams({ scope })}`, { signal }),
    refetchInterval: query => query.state.data?.batches.some(b => b.done + b.failed < b.total) ? 1500 : false,
  })
  if (!recent.data?.batches.length) return null
  return <details className="selection-note"><summary>{t('批次历史')}</summary>
    <ul>{recent.data.batches.map(b => <li key={b.id}>
      {b.action === 'download' ? t('下载所选') : b.action === 'subtitles' ? t('获取字幕') : t('补全所选番号')}
      {' · '}<code title={b.id}>{b.id.slice(0, 8)}</code>{' · '}
      {t('已处理 {settledImageCount} / {count}', { settledImageCount: b.done + b.failed, count: b.total })}
      {' · '}{t('失败')} {b.failed}{' · '}{t('已跳过')} {b.skipped}
    </li>)}</ul>
  </details>
}
