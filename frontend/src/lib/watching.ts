import { requestJson } from './api'
import { t } from './i18n'

export interface WatchSite { id: string; name: string; enabled: boolean; base_url: string }
export interface WatchEntry {
  source: string
  url: string
  code: string
  title: string
  cover: string
  editions: string[]
  description?: string
  recommendations?: WatchEntry[]
}
export type BookmarkKind = 'detail' | 'work' | 'playback'
export interface Bookmark extends WatchEntry { id: string; kind: BookmarkKind; created_at: number }
export interface Playback { session: string; media_url: string; media_kind: string; height: number | null; expires_in: number }
export interface WatchCatalog { items: WatchEntry[]; has_more: boolean; source: string; page: number }

export const bookmarkLabels: Record<BookmarkKind, string> = { detail: t('资料页'), work: t('站点作品'), playback: t('播放链接') }
export const bookmarkActionLabels: Record<BookmarkKind, string> = { detail: t('收藏资料页'), work: t('收藏站点作品'), playback: t('收藏播放链接') }
export function editionLabel(value: string) {
  const labels: Record<string, string> = { '原片': t('原片'), '中文字幕': t('中文字幕'), '无码流出': t('无码流出'), '无码': t('无码'), '英文字幕': t('英文字幕') }
  return labels[value] ?? value
}

export function watchHref(entry: Pick<WatchEntry, 'source' | 'url'>, play = false, query = ''): string {
  const params = new URLSearchParams({ source: entry.source, url: entry.url })
  if (play) params.set('play', '1')
  if (query) params.set('q', query)
  return `${play ? '/player' : '/watch'}?${params}`
}

export const watching = {
  recommendations: (source: string, url: string, signal?: AbortSignal) => requestJson<{items: WatchEntry[]}>(
    `/api/watch/recommendations?${new URLSearchParams({ source, url })}`, { signal, timeoutMs: 45_000 },
  ),
  sites: () => requestJson<{ sites: WatchSite[] }>('/api/watch/sites'),
  catalog: (source: string, q: string, page: number, signal?: AbortSignal) => requestJson<WatchCatalog>(
    `/api/watch/catalog?${new URLSearchParams({ source, q, page: String(page) })}`, { signal, timeoutMs: 65_000 },
  ),
  detail: (source: string, url: string, signal?: AbortSignal) => requestJson<WatchEntry>(
    `/api/watch/detail?${new URLSearchParams({ source, url })}`, { signal, timeoutMs: 65_000 },
  ),
  resolve: (entry: WatchEntry, signal?: AbortSignal) => requestJson<Playback>('/api/watch/resolve', {
    method: 'POST', body: JSON.stringify({ source: entry.source, url: entry.url }), timeoutMs: 85_000, signal,
  }),
  close: (session: string) => requestJson('/api/watch/close', { method: 'POST', body: JSON.stringify({ session }) }),
  bookmarks: () => requestJson<{ items: Bookmark[] }>('/api/bookmarks'),
  save: (entry: WatchEntry, kind: BookmarkKind) => requestJson<{ id: string }>('/api/bookmarks/action', {
    method: 'POST', body: JSON.stringify({ ...entry, kind, action: 'save' }),
  }),
  remove: (id: string) => requestJson('/api/bookmarks/action', { method: 'POST', body: JSON.stringify({ id, action: 'remove' }) }),
}
