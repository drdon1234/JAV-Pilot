import { useQuery } from '@tanstack/react-query'
import { api } from '../lib/api'
import { codeKey } from '../lib/localPlayback'
import { t } from '../lib/i18n'
import type {
  DownloadHistoryItem,
  WebDownloadBatch,
  WebDownloadBatchItem,
  WebDownloadBatchItemIntent,
} from '../types'

const labels = {
  create: t('预计新建'),
  skip: t('预计跳过'),
  replace: t('已有版本待替换'),
  pending: t('提交时核实'),
  excluded: t('未选择'),
}
type Impact = keyof typeof labels

export function estimateImpact(
  batch: WebDownloadBatch,
  item: WebDownloadBatchItem,
  intent: WebDownloadBatchItemIntent | undefined,
  history: DownloadHistoryItem | undefined,
  incomplete: boolean,
): Impact {
  if (!intent) return 'excluded'
  if (incomplete || !history) return 'pending'
  const editions = history.editions || []
  const matched = editions.filter((e) => e.variant === item.variant)
  if (matched.length && batch.existing_policy === 'skip') return 'skip'
  if (matched.length && batch.existing_policy === 'overwrite') return 'replace'
  if (item.quality_status !== 'ready' || history.web === 'active') return 'pending'
  const height =
    intent.quality_strategy === 'selected'
      ? intent.requested_height
      : Math.max(0, ...item.available_heights.filter((h) => h <= intent.requested_height))
  if (matched.length) {
    if (!height || matched.some((e) => !e.quality_height)) return 'pending'
    return Math.max(...matched.map((e) => e.quality_height || 0)) >= height ? 'skip' : 'replace'
  }
  if (history.web === 'completed' || editions.some((e) => !e.variant)) return 'pending'
  return 'create'
}

export function BatchImpact({
  batch,
  intents,
}: {
  batch: WebDownloadBatch
  intents: WebDownloadBatchItemIntent[]
}) {
  const codes = batch.items.map((item) => item.code)
  const query = useQuery({
    queryKey: ['batch-impact', batch.batch_id, codes],
    queryFn: async () => {
      const items = new Map<string, DownloadHistoryItem>()
      let incomplete = false
      for (let i = 0; i < codes.length; i += 200) {
        const result = await api.downloadHistoryLookup(codes.slice(i, i + 200))
        incomplete ||= result.unavailable.some((source) => source === 'web' || source === 'library')
        result.items.forEach((item) => items.set(codeKey(item.code), item))
      }
      return { items, incomplete }
    },
    staleTime: 30000,
    retry: false,
  })
  const selected = new Map(intents.map((i) => [i.code, i]))
  const groups: Record<Impact, string[]> = { create: [], skip: [], replace: [], pending: [], excluded: [] }
  for (const item of batch.items) {
    const impact = estimateImpact(
      batch,
      item,
      selected.get(item.code),
      query.data?.items.get(codeKey(item.code)),
      query.data?.incomplete ?? true,
    )
    groups[impact].push(item.code)
  }
  return (
    <details className="web-batch-impact">
      <summary>
        {Object.entries(groups)
          .filter(([, list]) => list.length)
          .map(([group, list]) => labels[group as Impact] + ' ' + list.length)
          .join(' · ')}
      </summary>
      {Object.entries(groups)
        .filter(([, list]) => list.length)
        .map(([group, list]) => (
          <p key={group}>
            <strong>{labels[group as Impact]}</strong> {list.join('、')}
          </p>
        ))}
    </details>
  )
}
