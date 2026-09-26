import type { DownloadHistoryItem } from '../types'

/** A short phrase describing where a code already exists, or '' when nowhere. */
export function downloadHistorySummary(item: DownloadHistoryItem | undefined | null): string {
  if (!item) return ''
  const parts: string[] = []
  if (item.library) parts.push('媒体库已有')
  if (item.torrent === 'completed') parts.push('BT 已下载')
  else if (item.torrent === 'downloading' || item.torrent === 'paused') parts.push('BT 下载中')
  if (item.web === 'completed') parts.push('Web 已下载')
  else if (item.web === 'active') parts.push('Web 下载中')
  return parts.join('、')
}

export function downloadHistoryBadge(item: DownloadHistoryItem | undefined | null): { label: string; tone: 'success' | 'info' } | null {
  if (!item || item.state === 'none') return null
  return item.state === 'downloaded'
    ? { label: item.library ? '媒体库已有' : '已下载', tone: 'success' }
    : { label: '下载中', tone: 'info' }
}
