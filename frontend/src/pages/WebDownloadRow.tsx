import { ArrowDown, ArrowUp, GripVertical, Pause, Play, RefreshCw, RotateCcw, Trash2, X } from 'lucide-react'

import { Button, IconButton, ProgressBar, StatusBadge } from '../components/ui'
import { formatBytes, formatEta } from '../lib/format'
import {
  formatWebDownloadDateTime,
  webDownloadContinueHint,
  webDownloadProgress,
  webDownloadQualityLabel,
  webDownloadRetryStatus,
  webDownloadStatusLabel,
  webDownloadStatusTone,
  webDownloadVariantLabel,
} from '../lib/webDownloads'
import type { WebDownloadJob, WorkMagnet } from '../types'
import { DownloadRecoveryActions } from './DownloadRecovery'
import { t } from '../lib/i18n'

export type WebDownloadActionName = 'pause' | 'resume' | 'cancel' | 'retry' | 'restart' | 'remove'

function webProviderLabel(provider: WebDownloadJob['provider'] | WebDownloadJob['resolved_provider']): string {
  if (provider === 'jable') return 'JableTV'
  if (provider === 'supjav') return 'SupJav'
  if (provider === 'missav') return 'MissAV'
  return t('自动选择')
}

function webJobProviderLabel(job: WebDownloadJob): string {
  if (job.resolved_provider) return webProviderLabel(job.resolved_provider)
  if (job.provider === 'missav' && job.status !== 'completed') return t('自动选择')
  return webProviderLabel(job.provider)
}

export function WebDownloadRow({
  task,
  busy,
  pendingAction,
  reselecting,
  webStarting,
  smartStarting,
  smartFailure,
  removing,
  restarting,
  onRequestRemove,
  onCancelRemove,
  onRequestRestart,
  onCancelRestart,
  onAction,
  onSmartSelection,
  onProbeMagnets,
  onWebDownload,
  onMagnetDownload,
  magnetStarting,
  onPriority,
  onMoveUp,
  onMoveDown,
  canMoveUp,
  canMoveDown,
  dragging,
  onDragStart,
  onDragEnd,
  onDrop,
}: {
  task: WebDownloadJob
  busy: boolean
  pendingAction: WebDownloadActionName | null
  reselecting: boolean
  webStarting: boolean
  smartStarting: boolean
  smartFailure: string | null
  removing: boolean
  restarting: boolean
  onRequestRemove: () => void
  onCancelRemove: () => void
  onRequestRestart: () => void
  onCancelRestart: () => void
  onAction: (name: WebDownloadActionName) => void
  onSmartSelection: () => void
  onProbeMagnets: () => void
  onWebDownload: () => void
  onMagnetDownload: (magnet: WorkMagnet) => void
  magnetStarting: boolean
  onPriority: (priority: number) => void
  onMoveUp: () => void
  onMoveDown: () => void
  canMoveUp: boolean
  canMoveDown: boolean
  dragging: boolean
  onDragStart: () => void
  onDragEnd: () => void
  onDrop: () => void
}) {
  const progress = webDownloadProgress(task)
  const continueHint = webDownloadContinueHint(task)
  const retryStatus = webDownloadRetryStatus(task)
  const targetDescription = task.output_path || continueHint || t('等待生成文件')
  const archiveMissing = task.status === 'completed' && task.archive_status === 'missing'
  const archiveUnknown = task.status === 'completed' && task.archive_status === 'unknown'
  const archiveReplaced = task.status === 'completed' && task.archive_status === 'replaced'
  const displayStatus = archiveReplaced
    ? t('已被新版本替换')
    : archiveMissing
    ? t('文件已删除')
    : archiveUnknown
      ? t('归档状态待确认')
      : webDownloadStatusLabel(task.status)
  const displayTone = archiveReplaced
    ? 'neutral' as const
    : archiveMissing
    ? 'danger' as const
    : archiveUnknown
      ? 'neutral' as const
      : webDownloadStatusTone(task.status)
  const transferStatus = archiveReplaced
    ? t('此记录对应的文件已由更高画质版本替换')
    : archiveMissing
    ? t('下载已完成，归档文件缺失')
    : archiveUnknown
      ? t('下载已完成，等待归档核验')
      : retryStatus || displayStatus
  const targetStatus = archiveReplaced
    ? t('历史版本已替换')
    : archiveMissing
    ? t('归档目标缺失')
    : archiveUnknown
      ? t('归档暂时无法核验')
      : task.status === 'completed'
        ? t('已归档')
        : task.status === 'archiving'
          ? t('归档中')
          : task.status === 'retry_wait'
            ? t('等待自动恢复')
            : t('Web 暂存')
  const taskIdentity = `${task.code} ${webDownloadVariantLabel(task.variant)}`
  return (
    <div
      className={`web-download-row web-stage-${task.status}${dragging ? ' dragging' : ''}${archiveReplaced ? ' web-archive-replaced' : archiveMissing ? ' web-archive-missing' : archiveUnknown ? ' web-archive-unknown' : ''}`}
      role="row"
      draggable={task.status === 'queued'}
      onDragStart={onDragStart}
      onDragEnd={onDragEnd}
      onDragOver={(event) => {
        if (task.status === 'queued') event.preventDefault()
      }}
      onDrop={(event) => {
        event.preventDefault()
        onDrop()
      }}
    >
      <div className="web-download-name-cell" role="cell">
        <strong>{task.status === 'queued' ? <GripVertical className="web-queue-grip" aria-hidden="true" /> : null}{task.code}</strong>
        <div>
          <StatusBadge>{webJobProviderLabel(task)}</StatusBadge>
          <StatusBadge>{webDownloadVariantLabel(task.variant)}</StatusBadge>
          <StatusBadge>{webDownloadQualityLabel(task)}</StatusBadge>
          {task.publication_outcome === 'replaced' ? <StatusBadge tone="success">{t('已覆盖旧文件')}</StatusBadge> : null}
          <span>{formatWebDownloadDateTime(task.created_at)}</span>
          {(task.priority ?? 0) !== 0 ? <StatusBadge tone="info">{t('优先级 {priority}', { priority: task.priority })}</StatusBadge> : null}
        </div>
        {task.error ? <span className="task-issue">{task.error}</span> : null}
        {task.reselection ? (
          <DownloadRecoveryActions
            candidate={task.reselection}
            busy={busy}
            reselecting={reselecting}
            webStarting={webStarting}
            smartStarting={smartStarting}
            smartFailure={smartFailure}
            onSmartSelection={onSmartSelection}
            onProbeMagnets={onProbeMagnets}
            onWebDownload={onWebDownload}
            onMagnetDownload={onMagnetDownload}
            magnetStarting={magnetStarting}
          />
        ) : null}
      </div>
      <div className="web-download-progress-cell" role="cell">
        <div>
          <StatusBadge tone={displayTone}>{displayStatus}</StatusBadge>
          <strong>{Math.round(progress * 100)}%</strong>
        </div>
        <ProgressBar value={progress} label={t('{taskIdentity} Web 下载进度', { taskIdentity })} />
        <span>
          {formatBytes(task.downloaded_bytes)}
          {(task.total_bytes ?? 0) > 0 ? ` / ${formatBytes(task.total_bytes ?? 0)}` : ''}
        </span>
      </div>
      <div className="web-download-transfer-cell" role="cell">
        <strong>{formatBytes(task.speed, true)}</strong>
        <span>{(task.eta ?? 0) > 0 ? t('剩余 {eta}', { eta: formatEta(task.eta ?? 0) }) : transferStatus}</span>
      </div>
      <div className="web-download-target-cell" role="cell">
        <strong>{targetStatus}</strong>
        <span title={targetDescription}>{targetDescription}</span>
      </div>
      <div className="web-download-actions" role="cell">
        {task.can_pause ? (
          <IconButton label={t('暂停 {taskIdentity} 下载', { taskIdentity })} onClick={() => onAction('pause')} disabled={busy}>
            <Pause aria-hidden="true" />
          </IconButton>
        ) : null}
        {task.can_resume ? (
          <IconButton label={t('恢复 {taskIdentity} 下载', { taskIdentity })} onClick={() => onAction('resume')} disabled={busy}>
            <Play aria-hidden="true" />
          </IconButton>
        ) : null}
        {task.status === 'queued' || task.status === 'paused' ? (
          <label className="web-download-priority">
            <span className="sr-only">{t('{taskIdentity} 优先级', { taskIdentity })}</span>
            <select value={task.priority ?? 0} onChange={(event) => onPriority(Number(event.target.value))} disabled={busy}>
              {![-100, 0, 100].includes(task.priority ?? 0) ? <option value={task.priority}>{task.priority}</option> : null}
              <option value="100">{t('高')}</option>
              <option value="0">{t('普通')}</option>
              <option value="-100">{t('低')}</option>
            </select>
          </label>
        ) : null}
        {task.status === 'queued' ? (
          <>
            <IconButton label={t('上移 {taskIdentity}', { taskIdentity })} size="small" onClick={onMoveUp} disabled={busy || !canMoveUp}>
              <ArrowUp aria-hidden="true" />
            </IconButton>
            <IconButton label={t('下移 {taskIdentity}', { taskIdentity })} size="small" onClick={onMoveDown} disabled={busy || !canMoveDown}>
              <ArrowDown aria-hidden="true" />
            </IconButton>
          </>
        ) : null}
        {task.can_cancel ? (
          <IconButton label={t('取消 {taskIdentity} 下载', { taskIdentity })} onClick={() => onAction('cancel')} disabled={busy}>
            <X aria-hidden="true" />
          </IconButton>
        ) : null}
        {task.can_retry ? (
          <>
            <Button
              className="web-download-action-button"
              type="button"
              size="small"
              variant="ghost"
              title={t('复用可用断点继续 {taskIdentity}', { taskIdentity })}
              onClick={() => onAction('retry')}
              disabled={busy}
              aria-busy={pendingAction === 'retry'}
            >
              <RotateCcw className={pendingAction === 'retry' ? 'spin' : ''} aria-hidden="true" />
              {pendingAction === 'retry' ? t('正在继续') : t('继续下载')}
            </Button>
            <Button
              className="web-download-action-button"
              type="button"
              size="small"
              variant="ghost"
              title={t('清理断点并从头下载 {taskIdentity}', { taskIdentity })}
              onClick={onRequestRestart}
              disabled={busy}
            >
              <RefreshCw aria-hidden="true" />
              {t('从头开始')}
            </Button>
          </>
        ) : null}
        {task.can_remove ? (
          <IconButton label={t('删除 {taskIdentity} 下载记录', { taskIdentity })} className="danger-icon" onClick={onRequestRemove} disabled={busy}>
            <Trash2 aria-hidden="true" />
          </IconButton>
        ) : null}
      </div>
      {removing ? (
        <div className="inline-confirm-cell" role="cell">
          <div className="inline-confirm" role="alert">
            <strong>{t('删除“{taskIdentity}”记录并清理检查点?', { taskIdentity })}</strong>
            <div>
              <Button type="button" size="small" variant="danger" onClick={() => onAction('remove')} disabled={busy}>
                <Trash2 aria-hidden="true" />
                {t('删除并清理')}
              </Button>
              <Button type="button" size="small" variant="ghost" onClick={onCancelRemove} disabled={busy}>
                <X aria-hidden="true" />
                {t('取消')}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
      {restarting ? (
        <div className="inline-confirm-cell" role="cell">
          <div className="inline-confirm" role="alert">
            <strong>{t('放弃“{taskIdentity}”现有断点并从头开始?', { taskIdentity })}</strong>
            <span>{t('仅清理暂存片段和检查点，不会删除已归档文件。')}</span>
            <div>
              <Button type="button" size="small" variant="danger" onClick={() => onAction('restart')} disabled={busy}>
                <RefreshCw className={pendingAction === 'restart' ? 'spin' : ''} aria-hidden="true" />
                {pendingAction === 'restart' ? t('正在清理') : t('清理断点并重新开始')}
              </Button>
              <Button type="button" size="small" variant="ghost" onClick={onCancelRestart} disabled={busy}>
                <X aria-hidden="true" />
                {t('取消')}
              </Button>
            </div>
          </div>
        </div>
      ) : null}
    </div>
  )
}
