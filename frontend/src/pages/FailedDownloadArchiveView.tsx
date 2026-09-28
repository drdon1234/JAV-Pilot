import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, RefreshCw, Trash2, X } from 'lucide-react'
import { useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { useToast } from '../components/ToastProvider'
import { Button, EmptyState, IconButton, InlineNotice, SkeletonRows } from '../components/ui'
import { api } from '../lib/api'
import { useFailedDownloadDisposition } from './FailedDownloadDisposition'
import { currentLocale, t } from '../lib/i18n'

const ARCHIVE_PAGE_SIZE = 50

function formatArchivedAt(timestamp: number): string {
  return new Intl.DateTimeFormat(currentLocale(), {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(new Date(timestamp * 1_000))
}

function detailTarget(code: string) {
  return `/works/${encodeURIComponent(`code:${code}`)}?code=${encodeURIComponent(code)}`
}

export function FailedDownloadArchiveView() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [page, setPage] = useState(0)
  const [deleteCode, setDeleteCode] = useState('')
  const confirmationTriggerRef = useRef<HTMLButtonElement | null>(null)
  const failedDisposition = useFailedDownloadDisposition()
  const archive = useQuery({
    queryKey: ['failed-download-archive', page],
    queryFn: () => api.failedDownloadArchive(
      ARCHIVE_PAGE_SIZE,
      page * ARCHIVE_PAGE_SIZE,
    ),
    retry: false,
  })
  const pageCount = Math.max(1, Math.ceil((archive.data?.count ?? 0) / ARCHIVE_PAGE_SIZE))
  const deleteArchive = useMutation({
    mutationFn: api.deleteFailedDownloadArchive,
    onSuccess: (payload, code) => {
      setDeleteCode('')
      toast.push(
        payload.removed ? t('{code} 的失败归档已永久删除', { code }) : t('{code} 已不在失败归档中', { code }),
        'success',
      )
      void queryClient.invalidateQueries({ queryKey: ['failed-download-archive'] })
      requestAnimationFrame(() => document.getElementById('archive-download-panel')?.focus())
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  useEffect(() => {
    if (archive.data && page >= pageCount) setPage(Math.max(0, pageCount - 1))
  }, [archive.data, page, pageCount])

  const items = archive.data?.items ?? []
  const rangeText = useMemo(() => {
    const count = archive.data?.count ?? 0
    if (!count) return t('0 条记录')
    const first = page * ARCHIVE_PAGE_SIZE + 1
    const last = first + items.length - 1
    return `${first}-${last} / ${count}`
  }, [archive.data?.count, items.length, page])

  function closeConfirmation() {
    setDeleteCode('')
    requestAnimationFrame(() => confirmationTriggerRef.current?.focus())
  }

  return (
    <div id="archive-download-panel" role="tabpanel" aria-labelledby="download-view-archive" tabIndex={0}>
      <section className="downloads-workspace failed-archive-workspace" aria-label={t('失败归档列表')}>
        <div className="section-toolbar downloads-toolbar failed-archive-toolbar">
          <span className="failed-archive-count">{t('失败归档：{value}', { value: archive.data?.count ?? 0 })}</span>
          <div>
            {failedDisposition.trigger}
            <IconButton label={t('刷新失败归档')} size="small" onClick={() => void archive.refetch()} disabled={archive.isFetching}>
              <RefreshCw className={archive.isFetching ? 'spin' : ''} aria-hidden="true" />
            </IconButton>
          </div>
        </div>

        {failedDisposition.confirmation}
        {failedDisposition.outcomeNotice}

        {archive.isLoading ? <SkeletonRows count={6} /> : null}
        {archive.isError && !archive.data ? (
          <EmptyState
            role="alert"
            title={t('无法加载失败归档')}
            description={t('请稍后重试；现有下载任务不会受影响。')}
            action={(
              <Button type="button" onClick={() => void archive.refetch()} disabled={archive.isFetching}>
                <RefreshCw className={archive.isFetching ? 'spin' : ''} aria-hidden="true" />
                {t('重新加载')}
              </Button>
            )}
          />
        ) : null}
        {archive.isError && archive.data ? (
          <InlineNotice tone="warning" role="status">
            <span>{t('刷新失败，当前显示的是上次成功加载的归档。')}</span>
            <Button type="button" size="small" variant="ghost" onClick={() => void archive.refetch()} disabled={archive.isFetching}>
              <RefreshCw className={archive.isFetching ? 'spin' : ''} aria-hidden="true" />
              {t('重试')}
            </Button>
          </InlineNotice>
        ) : null}
        {!archive.isLoading && !archive.isError && !items.length ? (
          <EmptyState
            title={t('暂无失败归档')}
            description={t('归档失败任务后，这里只保留番号，方便稍后打开作品详情。')}
          />
        ) : null}
        {items.length ? (
          <div className="failed-archive-list" role="list">
            {items.map((item) => (
              <div className="failed-archive-item" role="listitem" key={item.code}>
                <div className="failed-archive-row">
                  <Link
                    className="failed-archive-code"
                    to={detailTarget(item.code)}
                    state={{ returnTo: '/downloads?view=archive' }}
                    aria-label={t('查看 {code} 作品详情', { code: item.code })}
                  >
                    {item.code}
                  </Link>
                  <time dateTime={new Date(item.archived_at * 1_000).toISOString()}>
                    {formatArchivedAt(item.archived_at)}
                  </time>
                  <IconButton
                    label={t('永久删除 {code} 的归档记录', { code: item.code })}
                    size="small"
                    className="danger-icon"
                    onClick={(event) => {
                      confirmationTriggerRef.current = event.currentTarget
                      setDeleteCode(item.code)
                    }}
                    disabled={deleteArchive.isPending || Boolean(deleteCode)}
                  >
                    <Trash2 aria-hidden="true" />
                  </IconButton>
                </div>
                {deleteCode === item.code ? (
                  <div className="failed-archive-delete-confirm failed-archive-row-confirm" role="group" aria-labelledby={`failed-archive-delete-${item.code}`}>
                    <div>
                      <strong id={`failed-archive-delete-${item.code}`}>{t('永久删除“{code}”的归档记录？', { code: item.code })}</strong>
                      <span>{t('只删除失败归档记录，不会删除视频、NFO 或图片。此操作无法撤销。')}</span>
                    </div>
                    <div>
                      <Button type="button" size="small" variant="danger" onClick={() => deleteArchive.mutate(item.code)} disabled={deleteArchive.isPending}>
                        <Trash2 aria-hidden="true" />
                        {deleteArchive.isPending ? t('正在永久删除') : t('永久删除记录')}
                      </Button>
                      <Button autoFocus type="button" size="small" variant="ghost" onClick={closeConfirmation} disabled={deleteArchive.isPending}>
                        <X aria-hidden="true" />
                        {t('保留归档')}
                      </Button>
                    </div>
                  </div>
                ) : null}
              </div>
            ))}
          </div>
        ) : null}

        {(archive.data?.count ?? 0) > ARCHIVE_PAGE_SIZE ? (
          <div className="history-pager">
            <IconButton label={t('上一页')} size="small" onClick={() => setPage((current) => Math.max(0, current - 1))} disabled={page === 0 || archive.isFetching}>
              <ChevronLeft aria-hidden="true" />
            </IconButton>
            <span>{rangeText}</span>
            <IconButton label={t('下一页')} size="small" onClick={() => setPage((current) => current + 1)} disabled={!archive.data?.has_more || archive.isFetching}>
              <ChevronRight aria-hidden="true" />
            </IconButton>
          </div>
        ) : null}
      </section>
    </div>
  )
}
