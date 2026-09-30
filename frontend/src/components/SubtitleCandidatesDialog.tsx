import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Check, Download, RefreshCw, Trash2, X } from 'lucide-react'
import { useEffect, useRef } from 'react'

import { useToast } from './ToastProvider'
import { Button, EmptyState, IconButton, InlineNotice, SkeletonRows, StatusBadge } from './ui'
import { api } from '../lib/api'
import { t } from '../lib/i18n'
import { serviceErrorMessage } from '../lib/presentation'
import {
  PROVIDER_LABELS,
  SUBTITLE_ACTIVE_STATUSES,
  durationDeltaLabel,
  subtitleReasonLabel,
  subtitleRejectionLabel,
  subtitleScriptLabel,
  subtitleStatusLabel,
  subtitleStatusTone,
} from '../lib/subtitles'
import type { SubtitleActionRequest, SubtitleJob } from '../types'

import '../styles/subtitles.css'

export type SubtitleDialogTarget =
  | { kind: 'job'; jobId: string; code: string }
  | { kind: 'entry'; entryId: string; code: string }

export function SubtitleCandidatesDialog({
  target,
  onClose,
}: {
  target: SubtitleDialogTarget | null
  onClose: () => void
}) {
  const dialogRef = useRef<HTMLDialogElement>(null)
  const toast = useToast()
  const queryClient = useQueryClient()

  useEffect(() => {
    const dialog = dialogRef.current
    if (!dialog) return
    if (target && !dialog.open) dialog.showModal()
    if (!target && dialog.open) dialog.close()
  }, [target])

  const entryId = target?.kind === 'entry' ? target.entryId : null
  const entry = useQuery({
    queryKey: ['subtitle-entry', entryId],
    queryFn: () => api.subtitleForEntry(entryId as string),
    enabled: entryId !== null,
    retry: false,
  })
  const jobId = target?.kind === 'job' ? target.jobId : entry.data?.job?.job_id ?? null
  const candidates = useQuery({
    queryKey: ['subtitle-candidates', jobId],
    queryFn: () => api.subtitleCandidates(jobId as string),
    enabled: jobId !== null,
    retry: false,
    refetchInterval: (query) => {
      const status = query.state.data?.job.status
      return status && SUBTITLE_ACTIVE_STATUSES.has(status) ? 3_000 : false
    },
  })
  const job: SubtitleJob | null = candidates.data?.job ?? entry.data?.job ?? null

  const refresh = () => {
    for (const key of ['subtitles', 'subtitle-candidates', 'subtitle-entry', 'media-library']) {
      void queryClient.invalidateQueries({ queryKey: [key] })
    }
  }

  const action = useMutation({
    mutationFn: (request: SubtitleActionRequest) => api.subtitleAction(request),
    onSuccess: (_payload, request) => {
      const message = request.action === 'select'
        ? t('已换用这份字幕')
        : request.action === 'remove'
          ? t('已删除字幕')
          : t('已加入字幕获取队列')
      toast.push(message, 'success')
      refresh()
    },
    onError: (error) => toast.push(serviceErrorMessage(error, t('字幕操作未完成，请稍后重试')), 'error'),
  })

  const multipart = entry.data?.multipart === true
  const busy = action.isPending || (job ? SUBTITLE_ACTIVE_STATUSES.has(job.status) : false)
  const loading = (entryId !== null && entry.isLoading) || candidates.isLoading
  const reason = subtitleReasonLabel(job?.reason ?? null)
  const items = candidates.data?.candidates ?? []

  const fetchAgain = () => {
    if (target?.kind === 'entry') action.mutate({ action: 'fetch', entry_id: target.entryId })
    else if (job) action.mutate({ action: 'retry', job_id: job.job_id })
  }

  return (
    <dialog
      ref={dialogRef}
      className="subtitle-dialog"
      aria-labelledby="subtitle-dialog-title"
      onClose={onClose}
      onCancel={onClose}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose()
      }}
    >
      {target ? (
        <div className="subtitle-dialog-body">
          <header className="subtitle-dialog-header">
            <div>
              <h2 id="subtitle-dialog-title">{t('{code} 的字幕', { code: target.code })}</h2>
              {job ? (
                <span className="subtitle-dialog-status">
                  <StatusBadge tone={subtitleStatusTone(job.status)}>{subtitleStatusLabel(job.status)}</StatusBadge>
                  {reason ? <span>{reason}</span> : null}
                </span>
              ) : null}
            </div>
            <IconButton label={t('关闭')} onClick={onClose}>
              <X aria-hidden="true" />
            </IconButton>
          </header>

          {job?.subtitle_path ? (
            <code className="subtitle-dialog-path" title={job.subtitle_path}>{job.subtitle_path}</code>
          ) : null}
          {multipart ? <InlineNotice tone="warning">{t('暂不支持为分段影片获取字幕。')}</InlineNotice> : null}

          <div className="subtitle-dialog-actions">
            <Button
              type="button"
              variant="primary"
              onClick={fetchAgain}
              disabled={busy || multipart || (target.kind === 'job' && !job)}
            >
              <RefreshCw className={busy ? 'spin' : ''} aria-hidden="true" />
              {job ? t('重新获取') : t('获取字幕')}
            </Button>
            {job?.status === 'completed' ? (
              <Button
                type="button"
                variant="danger"
                onClick={() => action.mutate({ action: 'remove', job_id: job.job_id })}
                disabled={action.isPending}
              >
                <Trash2 aria-hidden="true" />
                {t('删除字幕')}
              </Button>
            ) : null}
          </div>

          {loading ? <SkeletonRows count={3} /> : null}
          {!loading && items.length === 0 ? (
            <EmptyState
              title={job ? t('没有可显示的候选字幕') : t('还没有获取过字幕')}
              description={job
                ? t('重新获取会再次查询所有已启用的字幕来源。')
                : t('获取字幕会按番号查询已启用的字幕来源。')}
            />
          ) : null}
          {items.length ? (
            <ul className="subtitle-candidate-list" aria-label={t('候选字幕')}>
              {items.map((candidate) => {
                const delta = durationDeltaLabel(candidate.duration_delta_ms)
                return (
                  <li key={candidate.candidate_id} className={candidate.selected ? 'selected' : undefined}>
                    <div className="subtitle-candidate-main">
                      <strong title={candidate.file_name}>{candidate.file_name}</strong>
                      <span className="subtitle-candidate-tags">
                        <StatusBadge>{PROVIDER_LABELS[candidate.provider] ?? candidate.provider}</StatusBadge>
                        <StatusBadge>{subtitleScriptLabel(candidate.declared_script)}</StatusBadge>
                        {candidate.format ? <StatusBadge>{candidate.format.toUpperCase()}</StatusBadge> : null}
                        {candidate.machine_translated ? <StatusBadge tone="warning">{t('机翻')}</StatusBadge> : null}
                        {candidate.rejected ? (
                          <StatusBadge tone="danger">{subtitleRejectionLabel(candidate.rejected)}</StatusBadge>
                        ) : null}
                        {delta ? <span>{delta}</span> : null}
                      </span>
                    </div>
                    {candidate.selected ? (
                      <StatusBadge tone="success">
                        <Check aria-hidden="true" />
                        {t('正在使用')}
                      </StatusBadge>
                    ) : (
                      <Button
                        type="button"
                        size="small"
                        onClick={() => {
                          if (job) action.mutate({ action: 'select', job_id: job.job_id, candidate_id: candidate.candidate_id })
                        }}
                        disabled={!job || busy || candidate.format === null}
                      >
                        <Download aria-hidden="true" />
                        {t('使用此字幕')}
                      </Button>
                    )}
                  </li>
                )
              })}
            </ul>
          ) : null}
        </div>
      ) : null}
    </dialog>
  )
}
