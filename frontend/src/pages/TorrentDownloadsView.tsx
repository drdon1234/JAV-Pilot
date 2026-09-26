import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, RefreshCw, Search, Settings, X } from 'lucide-react'
import { useEffect, useMemo, useState, type ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { useToast } from '../components/ToastProvider'
import { Button, EmptyState, IconButton, InlineNotice, PageHeader, SkeletonRows, StatusBadge } from '../components/ui'
import { api } from '../lib/api'
import { formatBytes } from '../lib/format'
import { useDownloadReselection } from './DownloadRecovery'
import { TorrentImportTool } from './TorrentImportTool'
import { TorrentRow } from './TorrentRow'

const filters = [
  { value: 'all', label: '全部' },
  { value: 'downloading', label: '下载中' },
  { value: 'completed', label: '已完成' },
  { value: 'paused', label: '已暂停' },
  { value: 'errored', label: '异常' },
]

const TORRENT_PAGE_SIZE = 50

function normalizeTorrentHistorySearch(value: string): string | null {
  const normalized = value.normalize('NFKC').trim().toUpperCase()
  if (!normalized) return ''
  const safePrefix = /^[A-Z]{2,12}$/.test(normalized)
  if (
    normalized.length > 40
    || !/^[A-Z0-9._-]+$/.test(normalized)
    || !/[A-Z]/.test(normalized)
    || (!/[0-9]/.test(normalized) && !safePrefix)
  ) return null
  return normalized
}

export function TorrentDownloadsView({
  active,
  navigation,
}: {
  active: boolean
  navigation: ReactNode
}) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [filter, setFilter] = useState('all')
  const [torrentPage, setTorrentPage] = useState(0)
  const [torrentQueryInput, setTorrentQueryInput] = useState('')
  const [torrentQuery, setTorrentQuery] = useState('')
  const [torrentQueryError, setTorrentQueryError] = useState('')
  const [deleteHash, setDeleteHash] = useState('')
  const [deleteFiles, setDeleteFiles] = useState(false)
  const [busyHashes, setBusyHashes] = useState<Set<string>>(() => new Set())
  const reselection = useDownloadReselection()

  const status = useQuery({
    queryKey: ['downloader-status'],
    queryFn: api.downloaderStatus,
    enabled: active,
    refetchInterval: 30_000,
  })
  const tasks = useQuery({
    queryKey: ['downloads', filter, torrentQuery, torrentPage],
    queryFn: () => api.torrents(
      filter,
      TORRENT_PAGE_SIZE,
      torrentPage * TORRENT_PAGE_SIZE,
      torrentQuery || undefined,
    ),
    enabled: active && status.data?.configured === true,
    refetchInterval: (query) => {
      if (!status.data?.configured || status.data.ok !== true) return false
      const rows = query.state.data?.tasks ?? []
      return rows.some((task) => ['queued', 'downloading', 'checking'].includes(task.stage))
        ? 5_000
        : rows.some((task) => ['queued', 'running'].includes(task.reselection?.recovery?.discovery_status || ''))
          ? 2_000
        : 30_000
    },
  })
  const action = useMutation({
    mutationFn: ({ name, hash, removeFiles = false }: { name: string; hash: string; removeFiles?: boolean }) => api.torrentAction(name, [hash], removeFiles),
    onSuccess: (data, variables) => {
      if (variables.name === 'delete' && deleteHash === variables.hash) {
        setDeleteHash('')
        setDeleteFiles(false)
      }
      if (variables.name === 'delete' && data.metadata_warning) {
        toast.push('任务已删除，但关联元数据任务清理失败', 'info')
      } else {
        toast.push(variables.name === 'delete' ? '任务已删除' : '任务状态已更新', 'success')
      }
      void queryClient.invalidateQueries({ queryKey: ['downloads'] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })

  async function runTorrentAction(name: string, hash: string, removeFiles = false) {
    if (busyHashes.has(hash)) return
    setBusyHashes((current) => new Set(current).add(hash))
    try {
      await action.mutateAsync({ name, hash, removeFiles })
    } catch {
      // The mutation callback presents the actionable error.
    } finally {
      setBusyHashes((current) => {
        const next = new Set(current)
        next.delete(hash)
        return next
      })
    }
  }

  const summary = useMemo(() => {
    if (tasks.data?.summary) return tasks.data.summary
    const list = tasks.data?.tasks ?? []
    return {
      scope: 'category' as const,
      category: tasks.data?.category ?? 'jav',
      total: list.length,
      downloading: list.filter((item) => item.stage === 'downloading').length,
      completed: list.filter((item) => item.stage === 'completed').length,
      errors: list.filter((item) => item.stage === 'error' || item.issue).length,
      speed: list.reduce((total, item) => total + item.dlspeed, 0),
    }
  }, [tasks.data])
  const downloaderOnline = status.data?.ok ?? tasks.data?.ok ?? false
  const downloaderConfigured = status.data?.configured ?? tasks.data?.configured ?? false
  const torrentCount = tasks.data?.count ?? tasks.data?.tasks.length ?? 0
  const torrentPageCount = Math.max(1, Math.ceil(torrentCount / TORRENT_PAGE_SIZE))
  const torrentHasNext = tasks.data?.has_more ?? torrentPage + 1 < torrentPageCount

  useEffect(() => {
    if (!tasks.isLoading && torrentPage >= torrentPageCount) {
      setTorrentPage(Math.max(0, torrentPageCount - 1))
    }
  }, [tasks.isLoading, torrentPage, torrentPageCount])

  if (!active) return null

  return (
    <>
      <PageHeader
        title="下载任务"
        description="BT、Web 下载队列与失败任务归档"
        actions={(
          <>
            <StatusBadge tone={downloaderOnline ? 'success' : downloaderConfigured || status.isError ? 'warning' : 'neutral'}>
              {downloaderOnline ? `qB ${status.data?.version || '在线'}` : downloaderConfigured ? 'qB 连接异常' : status.isError ? 'qB 状态未知' : 'qB 未配置'}
            </StatusBadge>
            <IconButton
              label="刷新任务"
              onClick={() => void Promise.all([tasks.refetch(), status.refetch()])}
              disabled={tasks.isFetching || status.isFetching}
            >
              <RefreshCw className={tasks.isFetching ? 'spin' : ''} aria-hidden="true" />
            </IconButton>
          </>
        )}
      />
      {navigation}
      <div id="bt-download-panel" role="tabpanel" aria-labelledby="download-view-bt" tabIndex={0}>
      <TorrentImportTool downloaderOnline={downloaderOnline} downloaderChecking={status.isLoading} />
      {tasks.data?.ok ? (
        <section className="summary-strip" aria-label="任务摘要">
          <div>
            <span>JAV 分类任务</span>
            <strong>{summary.total}</strong>
          </div>
          <div>
            <span>下载中</span>
            <strong>{summary.downloading}</strong>
          </div>
          <div>
            <span>已完成</span>
            <strong>{summary.completed}</strong>
          </div>
          <div>
            <span>异常</span>
            <strong>{summary.errors}</strong>
          </div>
          <div>
            <span>总下载速度</span>
            <strong>{formatBytes(summary.speed, true)}</strong>
          </div>
        </section>
      ) : null}

      <section className="downloads-workspace" aria-label="下载任务列表">
        {tasks.isLoading || tasks.data?.ok ? (
          <div className="section-toolbar downloads-toolbar">
            <div className="segmented-control" aria-label="任务筛选">
              {filters.map((item) => (
                <button
                  type="button"
                  className={filter === item.value ? 'active' : ''}
                  aria-pressed={filter === item.value}
                  onClick={() => {
                    setFilter(item.value)
                    setTorrentPage(0)
                  }}
                  key={item.value}
                >
                  {item.label}
                </button>
              ))}
            </div>
            <div className="downloads-toolbar-actions">
              <span className="polling-label">活动任务每 5 秒刷新</span>
            </div>
          </div>
        ) : null}

        {tasks.isLoading || tasks.data?.ok ? (
          <form
            className="web-download-history-controls bt-download-history-controls"
            aria-label="搜索 BT 下载历史"
            onSubmit={(event) => {
              event.preventDefault()
              const normalized = normalizeTorrentHistorySearch(torrentQueryInput)
              if (normalized === null) {
                setTorrentQueryError('请输入合法的作品番号或番号前缀')
                return
              }
              setTorrentQueryError('')
              setTorrentQueryInput(normalized)
              setTorrentQuery(normalized)
              setTorrentPage(0)
            }}
          >
            <label className="web-download-history-search">
              <Search aria-hidden="true" />
              <span className="sr-only">搜索番号</span>
              <input
                type="search"
                value={torrentQueryInput}
                placeholder="搜索番号"
                autoComplete="off"
                autoCapitalize="characters"
                spellCheck={false}
                maxLength={40}
                aria-invalid={Boolean(torrentQueryError)}
                aria-describedby={torrentQueryError ? 'torrent-history-query-error' : undefined}
                onChange={(event) => {
                  setTorrentQueryInput(event.target.value)
                  if (torrentQueryError) setTorrentQueryError('')
                }}
              />
            </label>
            <Button type="submit" size="small" variant="ghost" disabled={tasks.isFetching}>搜索</Button>
            {torrentQuery ? (
              <Button
                type="button"
                size="small"
                variant="ghost"
                onClick={() => {
                  setTorrentQueryInput('')
                  setTorrentQuery('')
                  setTorrentQueryError('')
                  setTorrentPage(0)
                }}
              >
                <X aria-hidden="true" />
                清除搜索
              </Button>
            ) : null}
            {torrentQueryError ? (
              <span className="field-error" id="torrent-history-query-error" role="alert">
                {torrentQueryError}
              </span>
            ) : null}
          </form>
        ) : null}

        {tasks.isLoading ? <SkeletonRows count={6} /> : null}
        {tasks.isError && !tasks.data ? (
          <EmptyState
            role="alert"
            title="无法加载下载任务"
            description={(tasks.error as Error).message || '请检查服务状态后重试'}
            action={
              <Button onClick={() => void tasks.refetch()} disabled={tasks.isFetching}>
                <RefreshCw className={tasks.isFetching ? 'spin' : ''} aria-hidden="true" />
                {tasks.isFetching ? '重试中' : '重新加载'}
              </Button>
            }
          />
        ) : null}
        {tasks.isError && tasks.data ? (
          <InlineNotice tone="warning" role="status">
            <div>
              <span>自动刷新失败，正在显示上次获取的任务。</span>
              <Button size="small" variant="ghost" onClick={() => void tasks.refetch()} disabled={tasks.isFetching}>
                <RefreshCw className={tasks.isFetching ? 'spin' : ''} aria-hidden="true" />
                重试
              </Button>
            </div>
          </InlineNotice>
        ) : null}
        {status.data?.configured === false || (tasks.data && !tasks.data.ok && !tasks.data.configured) ? (
          <EmptyState
            role="status"
            title="尚未配置 qBittorrent"
            description="完成连接设置后，下载任务和归档状态会显示在这里。"
            action={
              <Link className="button button-primary button-normal" to="/settings#qbittorrent-settings">
                <Settings aria-hidden="true" />
                配置 qBittorrent
              </Link>
            }
          />
        ) : null}
        {tasks.data && !tasks.data.ok && tasks.data.configured ? (
          <EmptyState
            role="alert"
            title="无法连接 qBittorrent"
            description="请检查 Web API 地址、账号和网络连接，然后重新检测。"
            action={
              <div className="empty-state-actions">
                <Button onClick={() => void Promise.all([tasks.refetch(), status.refetch()])} disabled={tasks.isFetching || status.isFetching}>
                  <RefreshCw className={tasks.isFetching || status.isFetching ? 'spin' : ''} aria-hidden="true" />
                  重新检测
                </Button>
                <Link className="button button-primary button-normal" to="/settings#qbittorrent-settings">
                  <Settings aria-hidden="true" />
                  检查连接设置
                </Link>
              </div>
            }
          />
        ) : null}
        {tasks.data?.ok && !tasks.data.tasks.length ? (
          <EmptyState
            title={filter === 'all' && !torrentQuery ? '还没有下载任务' : '没有匹配的下载任务'}
            description={filter === 'all' && !torrentQuery ? '从搜索结果解析磁链并添加后，任务会在这里自动刷新并按规则归档。' : '调整状态或番号筛选条件后重试。'}
            action={
              <Link className="button button-primary button-normal" to="/search">
                前往搜索
              </Link>
            }
          />
        ) : null}

        {tasks.data?.tasks.length ? (
          <div className="torrent-table" role="table" aria-label="qBittorrent 任务">
            <div className="torrent-table-head" role="row">
              <span role="columnheader">任务</span>
              <span role="columnheader">进度</span>
              <span role="columnheader">传输</span>
              <span role="columnheader">归档目标</span>
              <span role="columnheader">操作</span>
            </div>
            {tasks.data.tasks.map((task) => (
              <TorrentRow
                task={task}
                busy={busyHashes.has(task.hash)}
                reselecting={reselection.pending(task.reselection)}
                webStarting={reselection.webPending(task.reselection)}
                smartStarting={reselection.smartPending(task.reselection)}
                smartFailure={reselection.smartError(task.reselection)}
                deleting={deleteHash === task.hash}
                deleteFiles={deleteFiles}
                onDeleteFiles={setDeleteFiles}
                onRequestDelete={() => {
                  setDeleteHash(task.hash)
                  setDeleteFiles(false)
                }}
                onCancelDelete={() => {
                  setDeleteHash('')
                  setDeleteFiles(false)
                }}
                onAction={(name, removeFiles = false) => void runTorrentAction(name, task.hash, removeFiles)}
                onSmartSelection={() => task.reselection && reselection.startSmart(task.reselection)}
                onProbeMagnets={() => task.reselection && reselection.startProbe(task.reselection)}
                onWebDownload={() => task.reselection && reselection.startWeb(task.reselection)}
                onMagnetDownload={(magnet) => task.reselection && reselection.startMagnet(task.reselection, magnet)}
                magnetStarting={reselection.magnetPending(task.reselection)}
                key={task.hash}
              />
            ))}
          </div>
        ) : null}
        {tasks.data?.ok && torrentCount > 0 ? (
          <nav className="history-pager" aria-label="BT 下载任务分页">
            <IconButton
              label="上一页"
              size="small"
              onClick={() => setTorrentPage((page) => Math.max(0, page - 1))}
              disabled={torrentPage === 0 || tasks.isFetching}
            >
              <ChevronLeft aria-hidden="true" />
            </IconButton>
            <span>第 {torrentPage + 1} / {torrentPageCount} 页，共 {torrentCount} 条</span>
            <IconButton
              label="下一页"
              size="small"
              onClick={() => setTorrentPage((page) => page + 1)}
              disabled={!torrentHasNext || tasks.isFetching}
            >
              <ChevronRight aria-hidden="true" />
            </IconButton>
          </nav>
        ) : null}
      </section>
      </div>
    </>
  )
}
