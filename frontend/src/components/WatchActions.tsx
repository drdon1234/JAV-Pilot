import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Bookmark as BookmarkIcon, Check, Play } from 'lucide-react'
import { Link } from 'react-router-dom'

import { bookmarkActionLabels, watching, type BookmarkKind, type WatchEntry } from '../lib/watching'
import { t } from '../lib/i18n'
import { useToast } from './ToastProvider'
import { Button } from './ui'

export function WatchLink({ code }: { code: string }) {
  return <Link className="button button-secondary button-small" to={`/watch?q=${encodeURIComponent(code)}`}>
    <Play aria-hidden="true" />{t('在线观看')}
  </Link>
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
