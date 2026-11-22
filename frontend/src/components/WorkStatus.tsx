import { useQuery } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { api } from '../lib/api'
import { t } from '../lib/i18n'
import { codeKey, useLocalPlayback } from '../lib/localPlayback'
import { downloadHistorySummary } from '../lib/downloadHistory'
import { subtitleStatusLabel } from '../lib/subtitles'
import { webDownloadVariantLabel, webDownloadStatusLabel } from '../lib/webDownloads'
import { StatusBadge } from './ui'
import { metadataJobLabels } from '../lib/metadataStatus'

export function InventoryStatus({ code }: { code: string }) {
  const q = useLocalPlayback(code)
  if (q.isError) return <StatusBadge tone="warning">{t('馆藏状态暂不可用')}</StatusBadge>
  if (!q.data) return null
  const labels = (q.data.editions || [])
    .map((e) =>
      [e.quality_height && e.quality_height + 'p', e.variant && webDownloadVariantLabel(e.variant)]
        .filter(Boolean)
        .join(' '),
    )
    .filter(Boolean)
  const summary = [downloadHistorySummary(q.data), ...new Set(labels)].filter(Boolean).join(' · ')
  return summary ? <StatusBadge tone={q.data.library ? 'success' : 'info'}>{summary}</StatusBadge> : null
}

export function WorkProgress({ code }: { code: string }) {
  const inventory = useLocalPlayback(code)
  const web = useQuery({
    queryKey: ['work-web-jobs', code],
    queryFn: () => api.webDownloads({ code, limit: 20 }),
    refetchInterval: 15000,
    retry: false,
  })
  const meta = useQuery({
    queryKey: ['work-metadata', code],
    queryFn: () => api.mediaMetadata({ query: code, limit: 50 }),
    refetchInterval: 30000,
    retry: false,
  })
  const subs = useQuery({
    queryKey: ['work-subtitles', code],
    queryFn: () => api.subtitles({ query: code, limit: 50 }),
    refetchInterval: 30000,
    retry: false,
  })
  const metadata = meta.data?.jobs
    .filter((j) => codeKey(j.code) === codeKey(code))
    .sort((a, b) => b.updated_at - a.updated_at)[0]
  const subtitle = subs.data?.jobs
    .filter((j) => codeKey(j.code) === codeKey(code))
    .sort((a, b) => b.updated_at - a.updated_at)[0]
  const latest = web.data?.tasks[0]
  const editions = inventory.data?.editions || []
  const archived = Boolean(editions.length)
  const unknown = t('状态暂不可用'),
    waiting = t('读取中')
  const stage = (loading: boolean, failed: boolean, value: string) =>
    loading ? waiting : failed ? unknown : value
  const rows = [
    [
      t('下载'),
      stage(
        web.isPending || inventory.isPending,
        web.isError && inventory.isError,
        latest
          ? webDownloadStatusLabel(latest.status)
          : downloadHistorySummary(inventory.data) || t('暂无任务'),
      ),
      latest ? '/downloads?view=web' : '/downloads?view=torrent',
    ],
    [
      t('归档'),
      stage(inventory.isPending, inventory.isError, archived ? t('已入库') : t('暂无本地文件')),
      '/library?q=' + encodeURIComponent(code),
    ],
    [
      t('元数据'),
      stage(
        meta.isPending,
        meta.isError,
        metadata
          ? metadataJobLabels[metadata.status]
          : editions.some((e) => e.nfo_status === 'present')
            ? t('已有 NFO')
            : t('暂无任务'),
      ),
      '/metadata?code=' + encodeURIComponent(code),
    ],
    [
      t('字幕'),
      stage(
        subs.isPending,
        subs.isError,
        subtitle
          ? subtitleStatusLabel(subtitle.status)
          : editions.some((e) => e.subtitle_status === 'present')
            ? t('已有字幕')
            : t('暂无任务'),
      ),
      '/subtitles?q=' + encodeURIComponent(code),
    ],
  ]
  return (
    <section className="library-detail-files">
      <h2>{t('作品处理进度')}</h2>
      <dl className="library-detail-facts">
        {rows.map(([label, value, to]) => (
          <div key={label}>
            <dt>{label}</dt>
            <dd>
              <Link to={to}>{value}</Link>
            </dd>
          </div>
        ))}
      </dl>
    </section>
  )
}
