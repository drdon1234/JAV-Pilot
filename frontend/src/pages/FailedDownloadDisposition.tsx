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
import { t } from '../lib/i18n'

const CLEANUP_FAILURE_MESSAGES: Record<FailedDownloadCleanupErrorCode, string> = {
  source_changed: t('原任务状态已变化，请刷新后重新检查'),
  qb_cleanup_failed: t('qBittorrent 清理失败，请确认服务可用后重试'),
  web_cleanup_failed: t('Web 任务清理失败，请刷新后重试'),
  cleanup_storage_unavailable: t('任务存储暂不可用，请稍后重试'),
  cleanup_failed: t('清理未完成，请稍后重试'),
}

function cleanupFailureMessage(error: FailedDownloadCleanupErrorCode): string {
  return CLEANUP_FAILURE_MESSAGES[error] ?? t('清理未完成，请重新检查后重试')
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
        toast.push(t('没有符合条件的失败任务可处理'), 'success')
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
        payload.archived ? t('归档 {archived} 条', { archived: payload.archived }) : '',
        payload.deleted ? t('永久删除 {deleted} 条', { deleted: payload.deleted }) : '',
      ].filter(Boolean).join(t('，'))
      toast.push(
        handled > 0
          ? t('已{completed}{value}', { completed, value: payload.failed ? t('，另有 {failed} 条未处理', { failed: payload.failed }) : '' })
          : payload.failed
            ? t('{failed} 条失败任务未能处理，请查看详情后重试', { failed: payload.failed })
            : t('快照中的任务已被处理或不再符合条件'),
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
        toast.push(t('任务状态已变化或快照已失效，请重新检查后再处理'), 'info')
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
      {preview.isPending ? t('正在检查失败任务') : t('处理失败任务')}
    </Button>
  )
  const confirmation = snapshot ? (
    <div className="failed-download-disposition-confirm" role="group" aria-labelledby="failed-download-disposition-title">
      <div>
        <strong id="failed-download-disposition-title">{t('处理快照中的 {count} 条可清理失败任务', { count: snapshot.count })}</strong>
        <span>
          {t('范围包含全部 BT（{bt}）与 Web（{web}）任务，不受当前标签或筛选影响；不会删除视频、NFO 或图片。', { bt: snapshot.source_counts.bt, web: snapshot.source_counts.web })}
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
          {disposition.isPending && disposition.variables?.choice === 'archive' ? t('正在归档') : t('归档并移出任务列表')}
        </Button>
        <Button
          type="button"
          size="small"
          variant="danger"
          onClick={() => disposition.mutate({ choice: 'delete', snapshotToken: snapshot.snapshot_token })}
          disabled={disposition.isPending}
        >
          <Trash2 aria-hidden="true" />
          {disposition.isPending && disposition.variables?.choice === 'delete' ? t('正在永久删除') : t('永久删除记录')}
        </Button>
        <Button autoFocus type="button" size="small" variant="ghost" onClick={closeConfirmation} disabled={disposition.isPending}>
          <X aria-hidden="true" />
          {t('保留任务')}
        </Button>
      </div>
    </div>
  ) : null
  const outcomeNotice = lastResult?.failed ? (
    <InlineNotice tone="warning" role="status" className="failed-download-disposition-result">
      <strong>{t('{failed} 条任务未能处理', { failed: lastResult.failed })}</strong>
      {lastResult.failures.map((failure) => (
        <span key={failure.replacement_id}>{t('{code}：{value}', { code: failure.code, value: cleanupFailureMessage(failure.error) })}</span>
      ))}
      {lastResult.truncated ? <span>{t('仅显示前 {count} 条失败详情。', { count: lastResult.failures.length })}</span> : null}
    </InlineNotice>
  ) : null

  return { trigger, confirmation, outcomeNotice }
}
