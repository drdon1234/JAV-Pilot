import { Pause, Play, RotateCcw, Trash2, X } from 'lucide-react'

import { Button, IconButton, ProgressBar, StatusBadge, Toggle } from '../components/ui'
import { formatBytes, formatDateTime, formatEta } from '../lib/format'
import type { TorrentStage, TorrentTask, WorkMagnet } from '../types'
import { DownloadRecoveryActions } from './DownloadRecovery'
import { t } from '../lib/i18n'

const stageLabels: Record<TorrentStage, string> = {
  queued: t('排队中'),
  downloading: t('下载中'),
  checking: t('校验中'),
  paused: t('已暂停'),
  completed: t('已完成'),
  error: t('异常'),
}

function stageTone(stage: TorrentStage) {
  if (stage === 'completed') return 'success' as const
  if (stage === 'error') return 'danger' as const
  if (stage === 'paused') return 'warning' as const
  if (stage === 'downloading' || stage === 'checking') return 'info' as const
  return 'neutral' as const
}

export function TorrentRow({
  task,
  busy,
  reselecting,
  webStarting,
  smartStarting,
  smartFailure,
  deleting,
  deleteFiles,
  onDeleteFiles,
  onRequestDelete,
  onCancelDelete,
  onAction,
  onSmartSelection,
  onProbeMagnets,
  onWebDownload,
  onMagnetDownload,
  magnetStarting,
}: {
  task: TorrentTask
  busy: boolean
  reselecting: boolean
  webStarting: boolean
  smartStarting: boolean
  smartFailure: string | null
  deleting: boolean
  deleteFiles: boolean
  onDeleteFiles: (value: boolean) => void
  onRequestDelete: () => void
  onCancelDelete: () => void
  onAction: (name: string, removeFiles?: boolean) => void
  onSmartSelection: () => void
  onProbeMagnets: () => void
  onWebDownload: () => void
  onMagnetDownload: (magnet: WorkMagnet) => void
  magnetStarting: boolean
}) {
  return (
    <div className={`torrent-row stage-${task.stage}`} role="row">
      <div className="torrent-name-cell" role="cell">
        <strong title={task.name}>{task.name}</strong>
        <div>
          <StatusBadge tone={stageTone(task.stage)}>{stageLabels[task.stage]}</StatusBadge>
          <span>{formatDateTime(task.added_on)}</span>
        </div>
        {task.issue ? <span className="task-issue">{task.issue}</span> : null}
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
      <div className="torrent-progress-cell" role="cell">
        <div>
          <strong>{Math.round(task.progress * 100)}%</strong>
          <span>
            {formatBytes(task.downloaded)} / {formatBytes(task.size)}
          </span>
        </div>
        <ProgressBar value={task.progress} label={t('{name} 下载进度', { name: task.name })} />
      </div>
      <div className="torrent-transfer-cell" role="cell">
        <strong>{formatBytes(task.dlspeed, true)}</strong>
        <span>{t('剩余 {eta}', { eta: formatEta(task.eta) })}</span>
        {task.upspeed ? <span>{t('上传 {bytes}', { bytes: formatBytes(task.upspeed, true) })}</span> : null}
      </div>
      <div className="torrent-target-cell" role="cell">
        <strong>{task.category || t('未分类')}</strong>
        <span title={task.save_path}>{task.save_path || t('qB 默认路径')}</span>
        {task.tags ? <span>{task.tags}</span> : null}
      </div>
      <div className="torrent-actions" role="cell">
        {task.can_pause ? (
          <IconButton label={t('暂停任务：{name}', { name: task.name })} onClick={() => onAction('pause')} disabled={busy}>
            <Pause aria-hidden="true" />
          </IconButton>
        ) : null}
        {task.can_resume ? (
          <IconButton label={t('继续任务：{name}', { name: task.name })} onClick={() => onAction('resume')} disabled={busy}>
            <Play aria-hidden="true" />
          </IconButton>
        ) : null}
        <IconButton label={t('重新校验：{name}', { name: task.name })} onClick={() => onAction('recheck')} disabled={busy}>
          <RotateCcw aria-hidden="true" />
        </IconButton>
        <IconButton label={t('删除任务：{name}', { name: task.name })} className="danger-icon" onClick={onRequestDelete} disabled={busy}>
          <Trash2 aria-hidden="true" />
        </IconButton>
      </div>
      {deleting ? (
        <div className="inline-confirm" role="alert">
          <strong>{t('删除“{name}”?', { name: task.name })}</strong>
          <Toggle label={t('同时删除“{name}”的文件', { name: task.name })} checked={deleteFiles} onChange={(event) => onDeleteFiles(event.target.checked)} />
          <div>
            <Button type="button" size="small" variant="danger" aria-label={t('确认删除任务：{name}', { name: task.name })} onClick={() => onAction('delete', deleteFiles)} disabled={busy}>
              <Trash2 aria-hidden="true" />
              {t('确认删除')}
            </Button>
            <Button type="button" size="small" variant="ghost" aria-label={t('取消删除任务：{name}', { name: task.name })} onClick={onCancelDelete} disabled={busy}>
              <X aria-hidden="true" />
              {t('取消')}
            </Button>
          </div>
        </div>
      ) : null}
    </div>
  )
}
