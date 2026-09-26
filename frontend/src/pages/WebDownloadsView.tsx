import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { ChevronLeft, ChevronRight, RefreshCw, RotateCcw, Search, Trash2, X } from 'lucide-react'
import { lazy, Suspense, useEffect, useMemo, useRef, useState } from 'react'
import { Link } from 'react-router-dom'

import { useToast } from '../components/ToastProvider'
import { Button, EmptyState, IconButton, InlineNotice, SkeletonRows, StatusBadge } from '../components/ui'
import { api, ApiError } from '../lib/api'
import { formatBytes } from '../lib/format'
import { catalogHistoryQueryError } from '../lib/historyQuery'
import { recoverableImport } from '../lib/recoverableImport'
import {
  formatWebDownloadDateTime,
  WEB_DOWNLOAD_HISTORY_QUERY_ROOT,
  webDownloadAllHistoryIsStale,
  webDownloadBatchStatusLabels,
  webDownloadDisplayTasks,
  webDownloadHistoryQueryKey,
  webDownloadOccupiesSlot,
  webDownloadPollInterval,
} from '../lib/webDownloads'
import type {
  WebDownloadBatch,
  WebDownloadBatchStatus,
  WebDownloadListPayload,
} from '../types'
import { DownloadRecoveryActions, useDownloadReselection } from './DownloadRecovery'
import { WebDownloadRow, type WebDownloadActionName } from './WebDownloadRow'

const WebDownloadQueueControls = lazy(() => recoverableImport('src/pages/WebDownloadQueueControls.tsx', 'WebDownloadQueueControls', () => import('./WebDownloadQueueControls')).then((module) => ({
  default: module.WebDownloadQueueControls,
})))

const WebDownloadBatchTool = lazy(() => recoverableImport('src/pages/WebDownloadBatchTool.tsx', 'WebDownloadBatchTool', () => import('./WebDownloadBatchTool')).then((module) => ({
  default: module.WebDownloadBatchTool,
})))

const activeWebDownloadIntentStatuses = new Set<WebDownloadBatchStatus>(['queued', 'discovering', 'ready'])
const WEB_DOWNLOAD_PAGE_SIZE = 50

const webDownloadHistoryFilters = [
  { value: 'all', label: '全部状态' },
  { value: 'queued', label: '排队中' },
  { value: 'retry_wait', label: '等待恢复' },
  { value: 'downloading', label: '下载中' },
  { value: 'completed', label: '已完成' },
  { value: 'failed', label: '失败' },
  { value: 'cancelled', label: '已取消' },
]

export function WebDownloadsView() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [removeJobId, setRemoveJobId] = useState('')
  const [restartJobId, setRestartJobId] = useState('')
  const [pendingJobActions, setPendingJobActions] = useState<Map<string, WebDownloadActionName>>(() => new Map())
  const [cleanupConfirming, setCleanupConfirming] = useState(false)
  const [historyFilter, setHistoryFilter] = useState('all')
  const [historyQueryInput, setHistoryQueryInput] = useState('')
  const [historyQuery, setHistoryQuery] = useState('')
  const [historyPage, setHistoryPage] = useState(0)
  const historyQueryError = catalogHistoryQueryError(historyQueryInput)
  const [draggingJobId, setDraggingJobId] = useState('')
  const retryAllFailedLock = useRef(false)
  const reselection = useDownloadReselection()
  const tasks = useQuery({
    queryKey: webDownloadHistoryQueryKey(historyFilter, historyQuery, historyPage),
    queryFn: () => api.webDownloads({
      filter: historyFilter,
      query: historyQuery || undefined,
      limit: WEB_DOWNLOAD_PAGE_SIZE,
      offset: historyPage * WEB_DOWNLOAD_PAGE_SIZE,
    }),
    refetchInterval: (query) => {
      const jobs = query.state.data?.tasks ?? []
      const intents = query.state.data?.intents ?? []
      const activeIntervals = jobs
        .map((job) => webDownloadPollInterval(job, 2_000))
        .filter((interval): interval is number => interval !== false)
      if (jobs.some((job) => ['queued', 'running'].includes(job.reselection?.recovery?.discovery_status || ''))) {
        activeIntervals.push(2_000)
      }
      if (intents.some((intent) => activeWebDownloadIntentStatuses.has(intent.status))) {
        activeIntervals.push(2_000)
      }
      return activeIntervals.length > 0 ? Math.min(...activeIntervals) : 10_000
    },
    refetchIntervalInBackground: false,
    retry: false,
  })
  useEffect(() => {
    if (historyFilter === 'all' || historyPage !== 0 || !tasks.data) return
    const allHistoryKey = webDownloadHistoryQueryKey('all', historyQuery, 0)
    const current = queryClient.getQueryData<WebDownloadListPayload>(allHistoryKey)
    if (!current || !webDownloadAllHistoryIsStale(current, tasks.data)) return

    queryClient.removeQueries({
      queryKey: [...WEB_DOWNLOAD_HISTORY_QUERY_ROOT, 'all', historyQuery],
    })
    void queryClient.prefetchQuery({
      queryKey: allHistoryKey,
      queryFn: () => api.webDownloads({
        filter: 'all',
        query: historyQuery || undefined,
        limit: WEB_DOWNLOAD_PAGE_SIZE,
        offset: 0,
      }),
    })
  }, [historyFilter, historyPage, historyQuery, queryClient, tasks.data, tasks.dataUpdatedAt])
  const action = useMutation({
    mutationFn: ({ jobId, name }: { jobId: string; name: WebDownloadActionName }) => api.webDownloadAction(jobId, name),
    onSuccess: (_payload, variables) => {
      setRemoveJobId('')
      setRestartJobId('')
      const message = variables.name === 'cancel'
        ? '正在取消 Web 下载'
        : variables.name === 'pause'
          ? '正在暂停 Web 下载'
          : variables.name === 'resume'
            ? 'Web 下载已恢复排队'
        : variables.name === 'retry'
          ? 'Web 下载已继续排队'
          : variables.name === 'restart'
            ? '旧检查点已清理，Web 下载将从头开始'
          : 'Web 下载记录已删除，相关检查点已清理'
      toast.push(message, 'success')
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const retryIntent = useMutation({
    mutationFn: (intent: WebDownloadBatch) => api.webDownloadBatchAction(intent.batch_id, 'retry'),
    onSuccess: (_payload, intent) => {
      queryClient.setQueriesData<WebDownloadListPayload>(
        { queryKey: ['web-downloads'] },
        (current) => current?.intents
          ? {
              ...current,
              intents: current.intents.filter((item) => item.batch_id !== intent.batch_id),
            }
          : current,
      )
      toast.push(`${intent.code_or_prefix} 已重新加入后台`, 'success')
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const retryAllFailed = useMutation({
    mutationFn: api.retryFailedWebDownloads,
    onSuccess: async (payload) => {
      const result = payload.summary
      const failed = result.job_failed + result.intent_failed
      const truncated = result.truncated ? '；仍有超出本次上限的失败项' : ''
      toast.push(
        `Web 任务重试 ${result.job_retried}、失败 ${result.job_failed}；后台发现重试 ${result.intent_retried}、失败 ${result.intent_failed}${truncated}`,
        failed || result.truncated ? 'info' : 'success',
      )
      await queryClient.invalidateQueries({ queryKey: ['web-downloads'], refetchType: 'active' })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  function runRetryAllFailed() {
    if (retryAllFailedLock.current) return
    retryAllFailedLock.current = true
    retryAllFailed.mutate(undefined, {
      onSettled: () => {
        retryAllFailedLock.current = false
      },
    })
  }
  async function runAction(jobId: string, name: WebDownloadActionName) {
    if (pendingJobActions.has(jobId)) return
    setPendingJobActions((current) => new Map(current).set(jobId, name))
    try {
      await action.mutateAsync({ jobId, name })
    } catch {
      // The mutation callback presents the actionable error.
    } finally {
      setPendingJobActions((current) => {
        const next = new Map(current)
        next.delete(jobId)
        return next
      })
    }
  }
  const cleanupMissing = useMutation({
    mutationFn: api.cleanupMissingWebDownloads,
    onSuccess: (payload) => {
      setCleanupConfirming(false)
      toast.push(
        payload.removed > 0
          ? `已清空 ${payload.removed} 条文件已删除的 Web 下载记录`
          : '没有需要清空的 Web 下载记录',
        'success',
      )
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const queueUpdate = useMutation({
    mutationFn: ({
      kind,
      jobId,
      priority,
      jobIds,
      revision,
    }: {
      kind: 'priority' | 'reorder'
      jobId?: string
      priority?: number
      jobIds?: string[]
      revision: number
    }) => kind === 'priority'
      ? api.updateWebDownloadPriority(jobId || '', priority ?? 0, revision)
      : api.reorderWebDownloads(jobIds ?? [], revision),
    onSuccess: () => {
      toast.push('Web 下载队列顺序已更新', 'success')
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
    },
    onError: (error) => {
      toast.push(
        error instanceof ApiError && error.status === 409
          ? '队列已在其他位置变化，刷新后再调整'
          : (error as Error).message,
        'error',
      )
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
    },
  })
  const summary = useMemo(() => {
    const intents = tasks.data?.intents ?? []
    const queuedIntents = tasks.data?.queued_intent_count ?? intents.filter((intent) => (
      intent.status === 'queued' || intent.status === 'discovering' || intent.status === 'ready'
    )).length
    const visibleFailedIntents = intents.filter((intent) => (
      intent.status === 'failed' || intent.status === 'incomplete' || intent.status === 'too_many'
    )).length
    const failedIntents = tasks.data?.failed_intent_count ?? visibleFailedIntents
    if (tasks.data?.summary) {
      return {
        running: tasks.data.summary.running,
        queued: tasks.data.summary.queued + queuedIntents,
        retrying: tasks.data.summary.retrying ?? 0,
        completed: tasks.data.summary.completed,
        missing: tasks.data.summary.missing,
        errors: tasks.data.summary.failed + failedIntents,
        speed: tasks.data.summary.speed,
      }
    }
    const list = tasks.data?.tasks ?? []
    return {
      running: list.filter(webDownloadOccupiesSlot).length,
      queued: list.filter((item) => item.status === 'queued').length + queuedIntents,
      retrying: list.filter((item) => item.status === 'retry_wait').length,
      completed: list.filter((item) => item.status === 'completed' && item.archive_status === 'available').length,
      missing: list.filter((item) => item.status === 'completed' && item.archive_status === 'missing').length,
      errors: list.filter((item) => item.status === 'failed').length + failedIntents,
      speed: list.reduce((total, item) => total + Math.max(0, item.speed || 0), 0),
    }
  }, [tasks.data?.failed_intent_count, tasks.data?.intents, tasks.data?.queued_intent_count, tasks.data?.summary, tasks.data?.tasks])
  const serviceReady = Boolean(tasks.data?.ok && tasks.data.configured && tasks.data.available !== false)
  const historyCount = tasks.data?.count ?? tasks.data?.tasks.length ?? 0
  const historyPageCount = Math.max(1, Math.ceil(historyCount / WEB_DOWNLOAD_PAGE_SIZE))
  const historyHasNext = tasks.data?.has_more ?? historyPage + 1 < historyPageCount

  useEffect(() => {
    if (!tasks.isLoading && historyPage >= historyPageCount) {
      setHistoryPage(Math.max(0, historyPageCount - 1))
    }
  }, [historyPage, historyPageCount, tasks.isLoading])

  return (
    <div id="web-download-panel" role="tabpanel" aria-labelledby="download-view-web" tabIndex={0}>
      {tasks.data?.ok ? (
        <section className="summary-strip web-download-summary" aria-label="Web 下载摘要">
          <div><span>运行中</span><strong>{summary.running}{tasks.data.max_concurrency ? ` / ${tasks.data.max_concurrency}` : ''}</strong></div>
          <div><span>排队</span><strong>{summary.queued}</strong></div>
          <div><span>等待恢复</span><strong>{summary.retrying}</strong></div>
          <div><span>已完成</span><strong>{summary.completed}</strong></div>
          <div><span>文件已删除</span><strong>{summary.missing}</strong></div>
          <div><span>失败</span><strong>{summary.errors}</strong></div>
          <div><span>总下载速度</span><strong>{formatBytes(summary.speed, true)}</strong></div>
        </section>
      ) : null}

      <Suspense fallback={<section className="web-batch-tool" role="status" aria-label="正在加载 Web 批量下载工具"><SkeletonRows count={2} /></section>}>
        <WebDownloadBatchTool serviceReady={serviceReady} />
      </Suspense>

      {tasks.data?.control ? (
        <Suspense fallback={<section className="web-queue-controls" aria-label="正在加载队列控制"><SkeletonRows count={1} /></section>}>
          <WebDownloadQueueControls
            control={tasks.data.control}
            hardLimit={tasks.data.max_concurrency ?? 8}
            disabled={!tasks.data.configured}
          />
        </Suspense>
      ) : null}

      <section className="downloads-workspace web-downloads-workspace" aria-label="Web 下载任务列表">
        <div className="section-toolbar downloads-toolbar web-downloads-toolbar">
          <div>
            <StatusBadge tone={tasks.isLoading ? 'info' : serviceReady ? 'success' : tasks.isError || tasks.data?.configured ? 'warning' : 'neutral'}>
              {tasks.isLoading ? '正在检查 Web 下载' : serviceReady ? 'Web 下载可用' : tasks.isError ? 'Web 下载状态未知' : tasks.data?.configured ? 'Web 下载不可用' : 'Web 下载未配置'}
            </StatusBadge>
            <span className="polling-label">任务状态自动刷新</span>
          </div>
          <div className="web-download-toolbar-actions">
            {summary.errors > 0 ? (
              <Button
                type="button"
                size="small"
                variant="secondary"
                title="可继续的任务保留断点；无效断点会重新开始"
                onClick={runRetryAllFailed}
                disabled={retryAllFailed.isPending}
              >
                <RotateCcw className={retryAllFailed.isPending ? 'spin' : ''} aria-hidden="true" />
                {retryAllFailed.isPending ? '正在全部重试' : `全部重试失败项（${summary.errors}）`}
              </Button>
            ) : null}
            <Button
              type="button"
              size="small"
              variant="ghost"
              className="danger-icon"
              onClick={() => setCleanupConfirming(true)}
              disabled={cleanupConfirming || cleanupMissing.isPending}
            >
              <Trash2 aria-hidden="true" />
              一键清空已删除记录
            </Button>
            <IconButton label="刷新 Web 下载" onClick={() => void tasks.refetch()} disabled={tasks.isFetching || cleanupMissing.isPending}>
              <RefreshCw className={tasks.isFetching ? 'spin' : ''} aria-hidden="true" />
            </IconButton>
          </div>
        </div>

        <form
          className="web-download-history-controls"
          aria-label="筛选 Web 下载历史"
          onSubmit={(event) => {
            event.preventDefault()
            if (historyQueryError) return
            setHistoryPage(0)
            setHistoryQuery(historyQueryInput.trim())
          }}
        >
          <label>
            <span className="sr-only">任务状态</span>
            <select
              value={historyFilter}
              onChange={(event) => {
                setHistoryFilter(event.target.value)
                setHistoryPage(0)
              }}
            >
              {webDownloadHistoryFilters.map((item) => <option value={item.value} key={item.value}>{item.label}</option>)}
            </select>
          </label>
          <label className="web-download-history-search">
            <Search aria-hidden="true" />
            <span className="sr-only">搜索番号</span>
            <input
              type="search"
              value={historyQueryInput}
              placeholder="搜索番号"
              autoComplete="off"
              autoCapitalize="characters"
              spellCheck={false}
              maxLength={40}
              onChange={(event) => setHistoryQueryInput(event.target.value)}
              aria-invalid={Boolean(historyQueryError)}
              aria-describedby={historyQueryError ? 'web-download-history-query-error' : undefined}
            />
          </label>
          <Button type="submit" size="small" variant="ghost" disabled={tasks.isFetching || Boolean(historyQueryError)}>筛选</Button>
          {historyQueryError ? <span id="web-download-history-query-error" className="field-error" role="alert">{historyQueryError}</span> : null}
          {historyQuery ? (
            <Button
              type="button"
              size="small"
              variant="ghost"
              onClick={() => {
                setHistoryQueryInput('')
                setHistoryQuery('')
                setHistoryPage(0)
              }}
            >
              <X aria-hidden="true" />
              清除搜索
            </Button>
          ) : null}
        </form>

        {cleanupConfirming ? (
          <div className="web-download-cleanup-confirm" role="alert">
            <div>
              <strong>
                {summary.missing > 0
                  ? `清空至少 ${summary.missing} 条文件已删除的记录?`
                  : '扫描并清空文件已删除的记录?'}
              </strong>
              <span>只清除任务记录，不会删除 NAS 中的任何文件。</span>
            </div>
            <div>
              <Button type="button" size="small" variant="danger" onClick={() => cleanupMissing.mutate()} disabled={cleanupMissing.isPending}>
                <Trash2 aria-hidden="true" />
                {cleanupMissing.isPending ? '正在清空' : '确认清空记录'}
              </Button>
              <Button type="button" size="small" variant="ghost" onClick={() => setCleanupConfirming(false)} disabled={cleanupMissing.isPending}>
                <X aria-hidden="true" />
                取消
              </Button>
            </div>
          </div>
        ) : null}

        {tasks.isLoading ? <SkeletonRows count={5} /> : null}
        {tasks.isError && !tasks.data ? (
          <EmptyState
            role="alert"
            title="无法加载 Web 下载"
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
              <span>自动刷新失败，正在显示上次获取的 Web 任务。</span>
              <Button size="small" variant="ghost" onClick={() => void tasks.refetch()} disabled={tasks.isFetching}>
                <RefreshCw className={tasks.isFetching ? 'spin' : ''} aria-hidden="true" />
                重试
              </Button>
            </div>
          </InlineNotice>
        ) : null}
        {tasks.data?.intents?.length ? (
          <section className="web-download-intent-history" aria-label="后台发现记录">
            <div className="web-download-intent-history-heading">
              <strong>后台发现记录</strong>
              <span>番号已提交，但尚未生成 Web 下载任务的请求会保留在这里。</span>
            </div>
            <div className="web-download-intent-list">
              {tasks.data.intents.map((intent) => (
                <article className="web-download-intent-row" key={intent.batch_id}>
                  <div>
                    <strong>{intent.code_or_prefix}</strong>
                    <span>{formatWebDownloadDateTime(intent.updated_at)}</span>
                  </div>
                  <StatusBadge tone={
                    intent.status === 'failed' || intent.status === 'incomplete' || intent.status === 'too_many'
                      ? 'danger'
                      : intent.status === 'committed'
                        ? 'success'
                        : intent.status === 'cancelled' || intent.status === 'expired'
                          ? 'warning'
                          : 'info'
                  }
                  >{webDownloadBatchStatusLabels[intent.status]}</StatusBadge>
                  <span className="task-issue">{intent.error || (
                    activeWebDownloadIntentStatuses.has(intent.status)
                      ? '后台正在确认资源与分类'
                      : '尚未生成下载任务'
                  )}</span>
                  {intent.can_retry || intent.status === 'failed' || intent.status === 'incomplete' || intent.status === 'too_many' ? (
                    <div className="web-download-intent-actions">
                      <Button
                        type="button"
                        size="small"
                        variant="secondary"
                        disabled={retryIntent.isPending || reselection.pending(intent.reselection)}
                        onClick={() => retryIntent.mutate(intent)}
                      >
                        <RefreshCw className={retryIntent.isPending && retryIntent.variables?.batch_id === intent.batch_id ? 'spin' : ''} aria-hidden="true" />
                        重试
                      </Button>
                      {intent.reselection ? (
                        <DownloadRecoveryActions
                          candidate={intent.reselection}
                          busy={retryIntent.isPending}
                          reselecting={reselection.pending(intent.reselection)}
                          webStarting={reselection.webPending(intent.reselection)}
                          smartStarting={reselection.smartPending(intent.reselection)}
                          smartFailure={reselection.smartError(intent.reselection)}
                          onSmartSelection={() => reselection.startSmart(intent.reselection!)}
                          onProbeMagnets={() => reselection.startProbe(intent.reselection!)}
                          onWebDownload={() => reselection.startWeb(intent.reselection!)}
                          onMagnetDownload={(magnet) => reselection.startMagnet(intent.reselection!, magnet)}
                          magnetStarting={reselection.magnetPending(intent.reselection)}
                        />
                      ) : null}
                    </div>
                  ) : null}
                </article>
              ))}
            </div>
          </section>
        ) : null}
        {tasks.data && !tasks.data.configured && !tasks.data.tasks.length && !tasks.data.intents?.length ? (
          <EmptyState
            role="status"
            title="Web 下载尚未配置"
            description="配置 Web 下载站点后，可从作品详情页按需创建任务。"
          />
        ) : null}
        {tasks.data && tasks.data.configured && (!tasks.data.ok || tasks.data.available === false) && !tasks.data.tasks.length && !tasks.data.intents?.length ? (
          <EmptyState
            role="alert"
            title="Web 下载服务暂不可用"
            description={tasks.data.reason || tasks.data.error || '请稍后重新检测服务状态。'}
            action={
              <Button onClick={() => void tasks.refetch()} disabled={tasks.isFetching}>
                <RefreshCw className={tasks.isFetching ? 'spin' : ''} aria-hidden="true" />
                重新检测
              </Button>
            }
          />
        ) : null}
        {tasks.data?.ok && serviceReady && !tasks.data.tasks.length && !tasks.data.intents?.length ? (
          <EmptyState
            title={historyFilter !== 'all' || historyQuery ? '没有匹配的 Web 下载任务' : '还没有 Web 下载任务'}
            description={historyFilter !== 'all' || historyQuery ? '调整状态或番号筛选条件后重试。' : '可从作品详情页按需创建 Web 下载任务。'}
            action={<Link className="button button-primary button-normal" to="/search">前往搜索</Link>}
          />
        ) : null}
        {(tasks.data?.tasks.length || tasks.data?.intents?.length) && !serviceReady ? (
          <InlineNotice tone="warning" role="status">
            Web 下载当前不可用，正在显示已有任务记录。
          </InlineNotice>
        ) : null}

        {tasks.data?.tasks.length ? (
          <div className="web-download-table" role="table" aria-label="Web 下载任务">
            <div className="web-download-table-head" role="row">
              <span role="columnheader">任务</span>
              <span role="columnheader">状态与进度</span>
              <span role="columnheader">传输</span>
              <span role="columnheader">归档目标</span>
              <span role="columnheader">操作</span>
            </div>
            {webDownloadDisplayTasks(tasks.data.tasks).map((task, _index, displayTasks) => {
              const queuedTasks = displayTasks.filter((item) => item.status === 'queued')
              const queuedIndex = queuedTasks.findIndex((item) => item.job_id === task.job_id)
              const revision = tasks.data?.control?.queue_revision
              const moveQueuedTask = (direction: -1 | 1) => {
                if (revision === undefined || queuedIndex < 0) return
                const target = queuedIndex + direction
                if (target < 0 || target >= queuedTasks.length) return
                const next = queuedTasks.map((item) => item.job_id)
                ;[next[queuedIndex], next[target]] = [next[target], next[queuedIndex]]
                queueUpdate.mutate({ kind: 'reorder', jobIds: next, revision })
              }
              const dropQueuedTask = () => {
                if (!draggingJobId || draggingJobId === task.job_id || revision === undefined || queuedIndex < 0) return
                const next = queuedTasks.map((item) => item.job_id)
                const source = next.indexOf(draggingJobId)
                if (source < 0) return
                next.splice(source, 1)
                next.splice(queuedIndex, 0, draggingJobId)
                setDraggingJobId('')
                queueUpdate.mutate({ kind: 'reorder', jobIds: next, revision })
              }
              return (
              <WebDownloadRow
                task={task}
                busy={pendingJobActions.has(task.job_id)}
                pendingAction={pendingJobActions.get(task.job_id) ?? null}
                reselecting={reselection.pending(task.reselection)}
                webStarting={reselection.webPending(task.reselection)}
                smartStarting={reselection.smartPending(task.reselection)}
                smartFailure={reselection.smartError(task.reselection)}
                removing={removeJobId === task.job_id}
                restarting={restartJobId === task.job_id}
                onRequestRemove={() => setRemoveJobId(task.job_id)}
                onCancelRemove={() => setRemoveJobId('')}
                onRequestRestart={() => setRestartJobId(task.job_id)}
                onCancelRestart={() => setRestartJobId('')}
                onAction={(name) => void runAction(task.job_id, name)}
                onSmartSelection={() => task.reselection && reselection.startSmart(task.reselection)}
                onProbeMagnets={() => task.reselection && reselection.startProbe(task.reselection)}
                onWebDownload={() => task.reselection && reselection.startWeb(task.reselection)}
                onMagnetDownload={(magnet) => task.reselection && reselection.startMagnet(task.reselection, magnet)}
                magnetStarting={reselection.magnetPending(task.reselection)}
                onPriority={(priority) => {
                  if (revision === undefined) return
                  queueUpdate.mutate({ kind: 'priority', jobId: task.job_id, priority, revision })
                }}
                onMoveUp={() => moveQueuedTask(-1)}
                onMoveDown={() => moveQueuedTask(1)}
                canMoveUp={queuedIndex > 0}
                canMoveDown={queuedIndex >= 0 && queuedIndex < queuedTasks.length - 1}
                dragging={draggingJobId === task.job_id}
                onDragStart={() => setDraggingJobId(task.job_id)}
                onDragEnd={() => setDraggingJobId('')}
                onDrop={dropQueuedTask}
                key={task.job_id}
              />
              )
            })}
          </div>
        ) : null}
        {tasks.data?.ok && historyCount > 0 ? (
          <nav className="history-pager" aria-label="Web 下载历史分页">
            <IconButton
              label="上一页"
              size="small"
              onClick={() => setHistoryPage((page) => Math.max(0, page - 1))}
              disabled={historyPage === 0 || tasks.isFetching}
            >
              <ChevronLeft aria-hidden="true" />
            </IconButton>
            <span>第 {historyPage + 1} / {historyPageCount} 页，共 {historyCount} 条</span>
            <IconButton
              label="下一页"
              size="small"
              onClick={() => setHistoryPage((page) => page + 1)}
              disabled={!historyHasNext || tasks.isFetching}
            >
              <ChevronRight aria-hidden="true" />
            </IconButton>
          </nav>
        ) : null}
      </section>
    </div>
  )
}
