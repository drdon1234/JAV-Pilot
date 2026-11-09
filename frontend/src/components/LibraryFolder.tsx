import { useEffect, useRef, useState } from 'react'
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Link } from 'react-router-dom'
import { Check, MoreHorizontal, X } from 'lucide-react'
import { requestJson } from '../lib/api'
import { t } from '../lib/i18n'
import { Button, IconButton, InlineNotice } from './ui'

interface Cover { entry_id: string; title: string; code: string; identity: string }
interface FolderCover { entries: Cover[]; custom: boolean }
const coverUrl = (entry: Cover) => `/api/library/artwork?${new URLSearchParams({ entry: entry.entry_id, thumbnail: '1', identity: entry.identity })}`
export const folderLabel = (prefix: string) => prefix === '__unidentified__' ? t('未识别番号') : prefix

export function LibraryFolder({ prefix, count, returnPath, revision }: { prefix: string; count: number; returnPath: string; revision: number }) {
  const element = useRef<HTMLElement>(null)
  const [visible, setVisible] = useState(false)
  const [edit, setEdit] = useState(false)
  useEffect(() => {
    const observer = new IntersectionObserver(([entry]) => { if (entry.isIntersecting) { setVisible(true); observer.disconnect() } }, { rootMargin: '400px' })
    if (element.current) observer.observe(element.current)
    return () => observer.disconnect()
  }, [])
  const cover = useQuery({ queryKey: ['folder-cover', prefix, revision], queryFn: ({ signal }) => requestJson<FolderCover>(`/api/library/folder-cover?${new URLSearchParams({ group: prefix })}`, { signal }), enabled: visible, staleTime: 60_000, retry: false })
  return <article ref={element} className="library-poster-card library-folder-card">
    <div className="library-folder-surface">
      <Link to={`/library?${new URLSearchParams({ group: prefix })}`} state={{ libraryReturnPath: returnPath }} className="library-poster-link" aria-label={`${folderLabel(prefix)} · ${t('{count} 部作品', { count })}`}>
        <Mosaic entries={cover.data?.entries || []} />
      </Link>
    </div>
    <div className="library-poster-meta library-folder-meta">
      <div className="library-folder-caption">
        <Link to={`/library?${new URLSearchParams({ group: prefix })}`} state={{ libraryReturnPath: returnPath }} className="library-folder-title"><strong>{folderLabel(prefix)}</strong></Link>
        <span className="library-folder-count">{t('{count} 部作品', { count })}</span>
      </div>
      <IconButton className="library-cover-menu" label={t('自定义 {prefix} 目录封面', { prefix: folderLabel(prefix) })} onClick={() => setEdit(true)}><MoreHorizontal aria-hidden="true" /></IconButton>
      {cover.isError ? <button className="library-cover-retry" onClick={() => void cover.refetch()}>{t('重新加载封面')}</button> : null}
    </div>
    {edit ? <CoverPicker prefix={prefix} initial={cover.data?.entries || []} onClose={() => setEdit(false)} /> : null}
  </article>
}

function Mosaic({ entries }: { entries: Cover[] }) {
  return <div className="library-cover-mosaic" aria-hidden="true">{Array.from({ length: 9 }, (_, index) => <span key={index}>{entries[index] ? <img loading="lazy" src={coverUrl(entries[index])} alt="" onError={event => { event.currentTarget.hidden = true }} /> : null}</span>)}</div>
}

function CoverPicker({ prefix, initial, onClose }: { prefix: string; initial: Cover[]; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null)
  const sentinel = useRef<HTMLDivElement>(null)
  const client = useQueryClient()
  const [selected, setSelected] = useState<Cover[]>(initial)
  const [search, setSearch] = useState('')
  const [query, setQuery] = useState('')
  useEffect(() => { dialog.current?.showModal() }, [])
  useEffect(() => { const timeout = window.setTimeout(() => setQuery(search), 300); return () => window.clearTimeout(timeout) }, [search])
  const candidates = useInfiniteQuery({ queryKey: ['folder-cover-candidates', prefix, query], initialPageParam: 0,
    queryFn: ({ pageParam, signal }) => requestJson<{items: Cover[]; has_more: boolean; next_offset: number}>(`/api/library/folder-cover?${new URLSearchParams({ group: prefix, candidates: '1', offset: String(pageParam), q: query })}`, { signal }),
    getNextPageParam: page => page.has_more ? page.next_offset : undefined, retry: false,
  })
  const entries = candidates.data?.pages.flatMap(page => page.items) || []
  useEffect(() => {
    if (!sentinel.current || !candidates.hasNextPage || candidates.isFetching || candidates.isError) return
    const observer = new IntersectionObserver(([entry]) => { if (entry.isIntersecting) void candidates.fetchNextPage() }, { root: dialog.current, rootMargin: '200px' })
    observer.observe(sentinel.current)
    return () => observer.disconnect()
  }, [candidates.hasNextPage, candidates.isFetching, candidates.isError, candidates.fetchNextPage])
  const save = useMutation({ mutationFn: (ids: string[]) => requestJson('/api/library/folder-cover/action', { method: 'POST', body: JSON.stringify({ group: prefix, entries: ids }) }),
    onSuccess: () => { void client.invalidateQueries({ queryKey: ['folder-cover', prefix] }); onClose() },
  })
  return <dialog ref={dialog} className="library-cover-dialog" aria-labelledby="cover-picker-title" onCancel={event => { event.preventDefault(); onClose() }}>
    <header><h2 id="cover-picker-title">{t('自定义目录封面')} · {folderLabel(prefix)}</h2><IconButton label={t('关闭')} onClick={onClose}><X aria-hidden="true" /></IconButton></header>
    <p>{t('选择最多 9 张竖版海报，按选择顺序排列；不足的位置自动补齐。')}</p>
    <div className="library-cover-selection">{Array.from({ length: 9 }, (_, index) => <button key={index} disabled={!selected[index] || save.isPending} aria-label={selected[index] ? t('移除第 {index} 张封面', { index: index + 1 }) : t('第 {index} 张封面', { index: index + 1 })} onClick={() => setSelected(previous => previous.filter((_, i) => i !== index))}>
      {selected[index] ? <img src={coverUrl(selected[index])} alt={selected[index].code} /> : <span>{index + 1}</span>}
    </button>)}</div>
    <label className="library-cover-search"><span>{t('搜索番号或标题')}</span><input type="search" value={search} onChange={event => setSearch(event.target.value)} /></label>
    <div className="library-cover-candidates">{entries.map(entry => {
      const order = selected.findIndex(item => item.entry_id === entry.entry_id)
      return <button key={entry.entry_id} type="button" disabled={save.isPending || order < 0 && selected.length >= 9} aria-pressed={order >= 0} onClick={() => setSelected(previous => order >= 0 ? previous.filter(item => item.entry_id !== entry.entry_id) : [...previous, entry])}>
        <img src={coverUrl(entry)} alt={entry.title || entry.code} loading="lazy" /><strong>{entry.code}</strong>{order >= 0 ? <span className="library-cover-selected"><Check aria-hidden="true" />{order + 1}</span> : null}
      </button>
    })}</div>
    <div ref={sentinel} role="status">{candidates.isFetching ? t('正在加载') : !entries.length ? t('暂无可用竖版海报') : null}</div>
    {candidates.isError ? <InlineNotice tone="warning">{t('无法加载封面，请重试。')}<Button onClick={() => void candidates.refetch()}>{t('重试')}</Button></InlineNotice> : null}
    {save.isError ? <InlineNotice tone="danger">{t('封面保存失败，请重试。')}</InlineNotice> : null}
    <footer><Button variant="ghost" disabled={save.isPending} onClick={() => save.mutate([])}>{t('恢复默认封面')}</Button><div><Button disabled={save.isPending} onClick={() => setSelected([])}>{t('重新选择')}</Button><Button variant="primary" disabled={save.isPending || !selected.length} onClick={() => save.mutate(selected.map(item => item.entry_id))}>{t('保存封面')}</Button></div></footer>
  </dialog>
}
