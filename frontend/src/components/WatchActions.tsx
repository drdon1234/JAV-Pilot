import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bookmark as BookmarkIcon, Check, Play } from 'lucide-react'
import { Link } from 'react-router-dom'

import { bookmarkActionLabels, watching, type BookmarkKind, type WatchEntry } from '../lib/watching'
import { t } from '../lib/i18n'
import { useToast } from './ToastProvider'
import { Button } from './ui'
import type { DownloadHistoryItem } from '../types'
import { useLocalPlayback } from '../lib/localPlayback'

export function workAction(code: string, item?: DownloadHistoryItem | null) {
  const q = encodeURIComponent(code)
  if (item?.resume) {
    const {entry,file,identity} = item.resume
    return {to:'/player?'+new URLSearchParams({entry,file:String(file),identity}),label:t('继续播放')}
  }
  const entries = item?.local_entries || []
  if (entries.length) return {to:entries.length===1 ? '/player?entry='+encodeURIComponent(entries[0]) : '/library?sort=ingested&q='+q,label:entries.length===1 ? t('本地播放') : t('选择本地版本')}
  if (item && (item.state==='active' || item.web==='active' || item.torrent==='downloading' || item.torrent==='paused')) return {to:'/downloads?view='+(item.web==='active'?'web':'bt')+'&q='+q,label:t('查看下载任务')}
  return {to:'/watch?q='+q,label:t('找资源')}
}

export function LocalPlaybackLink({ code, showCode = false }: { code: string; showCode?: boolean }) {
  const availability = useLocalPlayback(code)
  const entries = availability.data?.local_entries ?? []
  if (availability.isError || !entries.length) return null
  const action = workAction(code, availability.data)
  return <Link className="button button-secondary button-small" to={action.to} title={code}><Play aria-hidden="true" />{availability.data?.resume ? action.label : t('本地播放')}{showCode ? ` · ${code}` : ''}</Link>
}

export function WatchLink({ code, resources }: { code: string; resources?: string }) {
  const availability = useLocalPlayback(code)
  const action = workAction(code, availability.data)
  const fallback = action.to.startsWith('/watch?')
  const primary = fallback ? resources : action.to
  return <>{primary && (primary.startsWith('#')
    ? <a className="button button-primary button-small" href={primary}>{action.label}</a>
    : <Link className="button button-primary button-small" data-focus-return-key={'action:'+code} to={primary}><Play aria-hidden="true" />{action.label}</Link>)}
    <Link className="button button-secondary button-small" data-focus-return-key={'online:'+code} to={'/watch?q='+encodeURIComponent(code)}>{t('在线观看')}</Link></>
}

export function BookmarkButton({ entry, kind }: { entry: WatchEntry; kind: BookmarkKind }) {
  const client = useQueryClient()
  const toast = useToast()
  const bookmarks = useQuery({ queryKey: ['bookmarks'], queryFn: watching.bookmarks, staleTime: 30_000 })
  const saved = bookmarks.data?.items.find(item => item.kind === kind && item.url === entry.url && item.source === entry.source)
  const mutation = useMutation({
    mutationFn: () => saved ? watching.remove(saved.id) : watching.save(entry, kind),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['bookmarks'] })
      toast.push(saved ? t('已取消收藏') : t('已加入收藏'), 'success')
    },
    onError: (error: Error) => toast.push(error.message, 'error'),
  })
  return <Button type="button" size="small" disabled={mutation.isPending || bookmarks.isPending || bookmarks.isError}
    aria-pressed={Boolean(saved)} onClick={() => mutation.mutate()} title={bookmarks.isError ? t('收藏加载失败，请进入收藏页重试') : undefined}>
    {saved ? <Check aria-hidden="true" /> : <BookmarkIcon aria-hidden="true" />}
    {saved ? t('已收藏') : bookmarkActionLabels[kind]}
  </Button>
}
