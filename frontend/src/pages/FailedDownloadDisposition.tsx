import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Archive, Trash2, X } from 'lucide-react'
import { useRef, useState } from 'react'

import { useToast } from '../components/ToastProvider'
import { Button, InlineNotice } from '../components/ui'
import { api, ApiError } from '../lib/api'
import type {
  FailedDownloadCleanupErrorCode,
  FailedDownloadDispositionPayload,
  FailedDownloadDispositionPreviewPayload,
} from '../types'

const CLEANUP_FAILURE_MESSAGES: Record<FailedDownloadCleanupErrorCode, string> = {
  source_changed: '原任务状态已变化，请刷新后重新检查',
  qb_cleanup_failed: 'qBittorrent 清理失败，请确认服务可用后重试',
  web_cleanup_failed: 'Web 任务清理失败，请刷新后重试',
  cleanup_storage_unavailable: '任务存储暂不可用，请稍后重试',
  cleanup_failed: '清理未完成，请稍后重试',
}

function cleanupFailureMessage(error: FailedDownloadCleanupErrorCode): string {
  return CLEANUP_FAILURE_MESSAGES[error] ?? '清理未完成，请重新检查后重试'
}

export function useFailedDownloadDisposition() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [snapshot, setSnapshot] = useState<FailedDownloadDispositionPreviewPayload | null>(null)
  const [lastResult, setLastResult] = useState<FailedDownloadDispositionPayload | null>(null)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const preview = useMutation({
    mutationFn: api.previewFailedDownloadDisposition,
    onSuccess: (payload) => {
      setLastResult(null)
      if (!payload.count) {
        toast.push('没有符合条件的失败任务可处理', 'success')
        return
      }
      setSnapshot(payload)
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const disposition = useMutation({
    mutationFn: ({ choice, snapshotToken }: { choice: 'archive' | 'delete'; snapshotToken: string }) => (
      api.disposeFailedDownloads(choice, snapshotToken)
    ),
    onSuccess: (payload) => {
      setSnapshot(null)
      setLastResult(payload)
      const handled = payload.archived + payload.deleted
      const completed = [
        payload.archived ? `归档 ${payload.archived} 条` : '',
        payload.deleted ? `永久删除 ${payload.deleted} 条` : '',
      ].filter(Boolean).join('，')
      toast.push(
        handled > 0
          ? `已${completed}${payload.failed ? `，另有 ${payload.failed} 条未处理` : ''}`
          : payload.failed
            ? `${payload.failed} 条失败任务未能处理，请查看详情后重试`
            : '快照中的任务已被处理或不再符合条件',
        payload.failed ? 'info' : 'success',
      )
      void queryClient.invalidateQueries({ queryKey: ['downloads'] })
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      void queryClient.invalidateQueries({ queryKey: ['failed-download-archive'] })
      requestAnimationFrame(() => triggerRef.current?.focus())
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setSnapshot(null)
        setLastResult(null)
        toast.push('任务状态已变化或快照已失效，请重新检查后再处理', 'info')
        requestAnimationFrame(() => triggerRef.current?.focus())
        return
      }
      toast.push((error as Error).message, 'error')
    },
  })

  function closeConfirmation() {
    setSnapshot(null)
    requestAnimationFrame(() => triggerRef.current?.focus())
  }

  const trigger = (
    <Button
      type="button"
      size="small"
      variant="ghost"
      onClick={(event) => {
        triggerRef.current = event.currentTarget
        preview.mutate()
      }}
      disabled={preview.isPending || disposition.isPending || snapshot !== null}
    >
      <Archive aria-hidden="true" />
      {preview.isPending ? '正在检查失败任务' : '处理失败任务'}
    </Button>
  )
  const confirmation = snapshot ? (
    <div className="failed-download-disposition-confirm" role="group" aria-labelledby="failed-download-disposition-title">
      <div>
        <strong id="failed-download-disposition-title">处理快照中的 {snapshot.count} 条可清理失败任务</strong>
        <span>
          范围包含全部 BT（{snapshot.source_counts.bt}）与 Web（{snapshot.source_counts.web}）任务，不受当前标签或筛选影响；不会删除视频、NFO 或图片。
        </span>
      </div>
      <div>
        <Button
          type="button"
          size="small"
          variant="secondary"
          onClick={() => disposition.mutate({ choice: 'archive', snapshotToken: snapshot.snapshot_token })}
          disabled={disposition.isPending}
        >
          <Archive aria-hidden="true" />
          {disposition.isPending && disposition.variables?.choice === 'archive' ? '正在归档' : '归档并移出任务列表'}
        </Button>
        <Button
          type="button"
          size="small"
          variant="danger"
          onClick={() => disposition.mutate({ choice: 'delete', snapshotToken: snapshot.snapshot_token })}
          disabled={disposition.isPending}
        >
          <Trash2 aria-hidden="true" />
          {disposition.isPending && disposition.variables?.choice === 'delete' ? '正在永久删除' : '永久删除记录'}
        </Button>
        <Button autoFocus type="button" size="small" variant="ghost" onClick={closeConfirmation} disabled={disposition.isPending}>
          <X aria-hidden="true" />
          保留任务
        </Button>
      </div>
    </div>
  ) : null
  const outcomeNotice = lastResult?.failed ? (
    <InlineNotice tone="warning" role="status" className="failed-download-disposition-result">
      <strong>{lastResult.failed} 条任务未能处理</strong>
      {lastResult.failures.map((failure) => (
        <span key={failure.replacement_id}>{failure.code}：{cleanupFailureMessage(failure.error)}</span>
      ))}
      {lastResult.truncated ? <span>仅显示前 {lastResult.failures.length} 条失败详情。</span> : null}
    </InlineNotice>
  ) : null

  return { trigger, confirmation, outcomeNotice }
}
