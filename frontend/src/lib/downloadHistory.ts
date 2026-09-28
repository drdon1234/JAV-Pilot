import type { DownloadHistoryItem } from '../types'
import { t } from './i18n'

/** A short phrase describing where a code already exists, or '' when nowhere. */
export function downloadHistorySummary(item: DownloadHistoryItem | undefined | null): string {
  if (!item) return ''
  const parts: string[] = []
  if (item.library) parts.push(t('媒体库已有'))
  if (item.torrent === 'completed') parts.push(t('BT 已下载'))
  else if (item.torrent === 'downloading' || item.torrent === 'paused') parts.push(t('BT 下载中'))
  if (item.web === 'completed') parts.push(t('Web 已下载'))
  else if (item.web === 'active') parts.push(t('Web 下载中'))
  return parts.join(t('、'))
}

export function downloadHistoryBadge(item: DownloadHistoryItem | undefined | null): { label: string; tone: 'success' | 'info' } | null {
  if (!item || item.state === 'none') return null
  return item.state === 'downloaded'
    ? { label: item.library ? t('媒体库已有') : t('已下载'), tone: 'success' }
    : { label: t('下载中'), tone: 'info' }
}
