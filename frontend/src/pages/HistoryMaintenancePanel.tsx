import '../styles/history.css'
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import {
  Archive,
  Database,
  Download,
  FileJson,
  FileSpreadsheet,
  RefreshCw,
  Search,
  ShieldCheck,
  Trash2,
} from 'lucide-react'
import { type FormEvent, useEffect, useMemo, useState } from 'react'

import { useToast } from '../components/ToastProvider'
import {
  Button,
  EmptyState,
  Field,
  InlineNotice,
  SkeletonRows,
  StatusBadge,
} from '../components/ui'
import { ApiError, api } from '../lib/api'
import { formatBytes, formatDateTime } from '../lib/format'
import { saveBlob } from '../lib/saveBlob'
import { webDownloadVariantLabel } from '../lib/webDownloads'
import type {
  HistoryCleanupPayload,
  HistoryExportDownload,
  HistoryExportFormat,
  HistoryFilters,
  HistoryPreviewPayload,
  HistoryRetentionPolicy,
  HistoryRetentionScheduleConfig,
  HistoryTaskType,
  HistoryVacuumPayload,
  HistoryVacuumTarget,
} from '../types'
import { t } from '../lib/i18n'

const taskTypeLabels: Record<HistoryTaskType, string> = {
  web: t('Web 下载'),
  batch: t('批次链'),
  metadata: t('元数据'),
}

const statusOptions: Record<HistoryTaskType, Array<{ value: string; label: string }>> = {
  web: [
    { value: 'all', label: t('全部状态') },
    { value: 'completed', label: t('已完成') },
    { value: 'failed', label: t('失败') },
    { value: 'cancelled', label: t('已取消') },
  ],
  batch: [
    { value: 'all', label: t('全部状态') },
    { value: 'committed', label: t('已提交') },
    { value: 'failed', label: t('失败') },
    { value: 'cancelled', label: t('已取消') },
    { value: 'expired', label: t('已过期') },
    { value: 'incomplete', label: t('不完整') },
    { value: 'too_many', label: t('超出限制') },
  ],
  metadata: [
    { value: 'all', label: t('全部状态') },
    { value: 'completed', label: t('已完成') },
    { value: 'failed', label: t('失败') },
  ],
}

const statusLabels: Record<string, string> = {
  completed: t('已完成'),
  failed: t('失败'),
  cancelled: t('已取消'),
  committed: t('已提交'),
  expired: t('已过期'),
  incomplete: t('不完整'),
  too_many: t('超出限制'),
  mixed: t('混合状态'),
}

const skippedReasonLabels: Record<string, string> = {
  active_task: t('活动任务'),
  status_changed: t('状态已变化'),
  record_changed: t('记录已变化'),
  review_in_progress: t('正在审校'),
  incomplete_batch_chain: t('批次链不完整'),
  linked_batch_history: t('仍被批次引用'),
  linked_metadata_history: t('仍被元数据引用'),
  provenance_invalid: t('来源事实不可验证'),
  no_longer_matches: t('不再符合筛选'),
  limit_boundary: t('会拆分批次链'),
  missing: t('记录已不存在'),
}

const vacuumLabels: Record<HistoryVacuumTarget, string> = {
  web: t('Web 与批次数据库'),
  metadata: t('元数据数据库'),
  library: t('媒体索引数据库'),
}

const PREVIEW_RENDER_LIMIT = 200
const retentionTimezones = [
  'Asia/Shanghai',
  'Asia/Tokyo',
  'UTC',
  'Europe/London',
  'America/New_York',
] as const

const retentionErrorLabels: Record<string, string> = {
  cleanup_failed: t('最近自动清理失败'),
  configuration_invalid: t('自动保留配置无效'),
  interrupted: t('上次自动清理意外中断'),
  scheduler_unavailable: t('自动保留调度器不可用'),
  state_conflict: t('历史状态发生冲突'),
  state_unavailable: t('自动保留状态不可用'),
  storage_unavailable: t('历史存储不可用'),
}

function statusTone(status: string) {
  if (['completed', 'committed'].includes(status)) return 'success' as const
  if (['failed', 'incomplete', 'too_many'].includes(status)) return 'danger' as const
  if (['cancelled', 'expired', 'mixed'].includes(status)) return 'warning' as const
  return 'neutral' as const
}

function optionalTimestamp(value: string): number | undefined {
  if (!value) return undefined
  const timestamp = new Date(value).getTime() / 1000
  return Number.isFinite(timestamp) ? timestamp : undefined
}

function retentionValue(value: string): number | null | undefined {
  const clean = value.trim()
  if (!clean) return null
  const parsed = Number(clean)
  if (!Number.isInteger(parsed) || parsed < 1 || parsed > 36_500) return undefined
  return parsed
}

function SkipSummary({ skipped }: { skipped: HistoryPreviewPayload['skipped'] | HistoryCleanupPayload['skipped'] }) {
  const entries = Object.entries(skipped.counts).filter(([, count]) => count > 0)
  if (!entries.length) return null
  return (
    <div className="history-skip-summary" role="group" aria-label={t('跳过记录摘要')}>
      {entries.map(([reason, count]) => (
        <span key={reason}>
          {skippedReasonLabels[reason] || t('条件已变化')} {count}
        </span>
      ))}
      {skipped.details_truncated ? <span>{t('仅显示部分详情')}</span> : null}
    </div>
  )
}

function PreviewTable({ preview }: { preview: HistoryPreviewPayload }) {
  if (!preview.items.length) {
    return (
      <InlineNotice tone="warning" role="status">
        {t('没有可清理记录。活动任务、审校中记录和不完整批次链已保留。')}
      </InlineNotice>
    )
  }
  const visibleItems = preview.items.slice(0, PREVIEW_RENDER_LIMIT)
  return (
    <>
      <div className="history-table-wrap" role="region" tabIndex={0} aria-label={t('可横向滚动的历史清理预览表格')}>
        <table className="history-table" aria-label={t('历史清理预览')}>
          <thead>
            <tr>
              <th>{t('类型')}</th>
              <th>{t('番号 / 记录')}</th>
              <th>{t('状态')}</th>
              <th>{t('记录数')}</th>
              <th>{t('估算空间')}</th>
            </tr>
          </thead>
          <tbody>
            {visibleItems.map((item) => (
              <tr key={`${item.task_type}:${item.id}`}>
                <td>{taskTypeLabels[item.task_type]}</td>
                <td>
                  <strong>{item.code || t('未记录番号')}</strong>
                  {item.variant ? <StatusBadge>{webDownloadVariantLabel(item.variant)}</StatusBadge> : null}
                  <code title={item.id}>{item.id.slice(0, 12)}</code>
                </td>
                <td><StatusBadge tone={statusTone(item.status)}>{statusLabels[item.status] || item.status}</StatusBadge></td>
                <td>{item.record_count}</td>
                <td>{formatBytes(item.estimated_bytes)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      {preview.items.length > visibleItems.length ? (
        <span className="history-table-limit" role="status">
          {t('显示前 {count} 个分组，共 {count2} 个', { count: visibleItems.length, count2: preview.items.length })}
        </span>
      ) : null}
    </>
  )
}

/** The history maintenance tools shown in the Settings "维护" tab. */
export function HistoryMaintenancePanel({ active }: { active: boolean }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [types, setTypes] = useState<Record<HistoryTaskType, boolean>>({
    web: true,
    batch: true,
    metadata: true,
  })
  const [statuses, setStatuses] = useState<Record<HistoryTaskType, string>>({
    web: 'all',
    batch: 'all',
    metadata: 'all',
  })
  const [code, setCode] = useState('')
  const [timeField, setTimeField] = useState<'created' | 'updated'>('updated')
  const [timeAfter, setTimeAfter] = useState('')
  const [timeBefore, setTimeBefore] = useState('')
  const [limit, setLimit] = useState('1000')
  const [filterError, setFilterError] = useState('')
  const [preview, setPreview] = useState<HistoryPreviewPayload | null>(null)
  const [confirmCleanup, setConfirmCleanup] = useState(false)
  const [cleanupReport, setCleanupReport] = useState<HistoryCleanupPayload | null>(null)
  const [lastExport, setLastExport] = useState<HistoryExportDownload | null>(null)
  const [retention, setRetention] = useState<Record<HistoryTaskType, string>>({ web: '', batch: '', metadata: '' })
  const [retentionAutoEnabled, setRetentionAutoEnabled] = useState(false)
  const [retentionTimezone, setRetentionTimezone] = useState('Asia/Shanghai')
  const [retentionHour, setRetentionHour] = useState('3')
  const [retentionDirty, setRetentionDirty] = useState(false)
  const [retentionError, setRetentionError] = useState('')
  const [confirmRetentionRecovery, setConfirmRetentionRecovery] = useState(false)
  const [retentionRecoveryBackup, setRetentionRecoveryBackup] = useState('')
  const [vacuumTarget, setVacuumTarget] = useState<HistoryVacuumTarget>('web')
  const [confirmVacuum, setConfirmVacuum] = useState(false)
  const [vacuumReport, setVacuumReport] = useState<HistoryVacuumPayload | null>(null)
  const [now, setNow] = useState(() => Date.now() / 1000)

  const status = useQuery({
    queryKey: ['history-status'],
    queryFn: api.historyStatus,
    enabled: active,
    retry: false,
    refetchInterval: active ? 30_000 : false,
  })

  useEffect(() => {
    if (!status.data || retentionDirty) return
    setRetention({
      web: status.data.retention.web?.toString() || '',
      batch: status.data.retention.batch?.toString() || '',
      metadata: status.data.retention.metadata?.toString() || '',
    })
    setRetentionAutoEnabled(status.data.retention_schedule.auto_enabled)
    setRetentionTimezone(status.data.retention_schedule.timezone)
    setRetentionHour(status.data.retention_schedule.hour.toString())
  }, [retentionDirty, status.data])

  useEffect(() => {
    if (!preview) return
    setNow(Date.now() / 1000)
    const timer = window.setInterval(() => setNow(Date.now() / 1000), 1000)
    return () => window.clearInterval(timer)
  }, [preview])

  const clearPreview = () => {
    setPreview(null)
    setConfirmCleanup(false)
    setCleanupReport(null)
  }

  function buildFilters(maximum = status.data?.limits.cleanup_records ?? 10_000): HistoryFilters | null {
    const taskTypes = (Object.keys(types) as HistoryTaskType[]).filter((type) => types[type])
    if (!taskTypes.length) {
      setFilterError(t('至少选择一种任务类型'))
      return null
    }
    const cleanLimit = Number(limit)
    if (!Number.isInteger(cleanLimit) || cleanLimit < 1 || cleanLimit > maximum) {
      setFilterError(t('记录上限应为 1 至 {maximum}', { maximum }))
      return null
    }
    const after = optionalTimestamp(timeAfter)
    const before = optionalTimestamp(timeBefore)
    if ((timeAfter && after === undefined) || (timeBefore && before === undefined) || (after && before && after >= before)) {
      setFilterError(t('时间范围无效'))
      return null
    }
    const statusFilters: Partial<Record<HistoryTaskType, string[]>> = {}
    taskTypes.forEach((type) => {
      if (statuses[type] !== 'all') statusFilters[type] = [statuses[type]]
    })
    setFilterError('')
    return {
      task_types: taskTypes,
      ...(Object.keys(statusFilters).length ? { statuses: statusFilters } : {}),
      ...(after !== undefined ? { [`${timeField}_after`]: after } : {}),
      ...(before !== undefined ? { [`${timeField}_before`]: before } : {}),
      ...(code.trim() ? { code: code.trim() } : {}),
      limit: cleanLimit,
    } as HistoryFilters
  }

  const previewCleanup = useMutation({
    mutationFn: (filters: HistoryFilters) => api.previewHistory(filters),
    onSuccess: (payload) => {
      setPreview(payload)
      setConfirmCleanup(false)
      setCleanupReport(null)
    },
    onError: () => toast.push(t('无法生成历史清理预览，请稍后重试'), 'error'),
  })

  const executeCleanup = useMutation({
    mutationFn: (token: string) => api.executeHistoryCleanup(token),
    onSuccess: (payload) => {
      setPreview(null)
      setConfirmCleanup(false)
      setCleanupReport(payload)
      toast.push(t('已清理 {records} 条任务记录', { records: payload.removed.records }), 'success')
      void queryClient.invalidateQueries({ queryKey: ['history-status'] })
    },
    onError: (error) => {
      if (error instanceof ApiError && error.status === 409) {
        setPreview(null)
        setConfirmCleanup(false)
        toast.push(t('清理预览已失效，请重新生成后再执行'), 'error')
      } else {
        toast.push(t('清理请求未确认，请保留当前预览并重试'), 'error')
      }
    },
  })

  const exportHistory = useMutation({
    mutationFn: ({ filters, format }: { filters: HistoryFilters; format: HistoryExportFormat }) =>
      api.exportHistory(filters, format),
    onSuccess: (payload) => {
      saveBlob(payload.blob, payload.filename)
      setLastExport(payload)
      toast.push(t('{value} 历史已导出', { value: payload.format.toUpperCase() }), 'success')
    },
    onError: () => toast.push(t('历史导出失败，请缩小范围后重试'), 'error'),
  })

  const saveRetention = useMutation({
    mutationFn: ({ policy, schedule }: {
      policy: HistoryRetentionPolicy
      schedule: HistoryRetentionScheduleConfig
    }) => api.updateHistoryRetention(policy, schedule),
    onSuccess: (payload) => {
      queryClient.setQueryData(['history-status'], payload)
      setRetentionDirty(false)
      setRetentionError('')
      toast.push(t('历史保留策略已保存'), 'success')
    },
    onError: () => toast.push(t('无法保存历史保留策略'), 'error'),
  })

  const previewRetention = useMutation({
    mutationFn: (value: number) => api.previewHistoryRetention(value),
    onSuccess: (payload) => {
      setPreview(payload)
      setConfirmCleanup(false)
      setCleanupReport(null)
      toast.push(t('已按保留策略生成清理预览'), 'success')
    },
    onError: () => toast.push(t('无法按保留策略生成预览'), 'error'),
  })

  const recoverRetentionState = useMutation({
    mutationFn: () => api.recoverHistoryRetentionState(),
    onSuccess: (payload) => {
      queryClient.setQueryData(['history-status'], payload)
      setConfirmRetentionRecovery(false)
      setRetentionRecoveryBackup(payload.state_recovery.backup_name || '')
      toast.push(t('自动保留状态已恢复'), 'success')
    },
    onError: () => {
      setConfirmRetentionRecovery(false)
      toast.push(t('自动保留状态未恢复，请确认自动清理已关闭'), 'error')
    },
  })

  const vacuum = useMutation({
    mutationFn: (target: HistoryVacuumTarget) => api.vacuumHistory(target),
    onSuccess: (payload) => {
      setVacuumReport(payload)
      setConfirmVacuum(false)
      toast.push(t('已回收 {bytes} 数据库空间', { bytes: formatBytes(payload.reclaimed_bytes) }), 'success')
      void queryClient.invalidateQueries({ queryKey: ['history-status'] })
    },
    onError: () => {
      setConfirmVacuum(false)
      toast.push(t('数据库空间回收未执行，请检查维护状态与备份'), 'error')
    },
  })

  const retentionPolicy = useMemo<HistoryRetentionPolicy | null>(() => {
    const web = retentionValue(retention.web)
    const batch = retentionValue(retention.batch)
    const metadata = retentionValue(retention.metadata)
    if (web === undefined || batch === undefined || metadata === undefined) return null
    return { web, batch, metadata }
  }, [retention])
  const retentionEnabled = Boolean(status.data && Object.values(status.data.retention).some((value) => value !== null))
  const retentionSchedule = status.data?.retention_schedule
  const retentionAutomaticError = retentionSchedule?.last_error_code
    ? retentionErrorLabels[retentionSchedule.last_error_code] || t('最近自动清理未完成')
    : ''
  const retentionRecoveryRequired = Boolean(retentionSchedule?.state_recovery_required)
  const canRecoverRetentionState = Boolean(
    retentionRecoveryRequired
    && !retentionSchedule?.active
    && !retentionDirty,
  )
  const canVacuum = Boolean(status.data?.maintenance_mode && status.data.backup_verified)
  const previewExpired = Boolean(preview && now >= preview.expires_at)

  function submitPreview(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    const filters = buildFilters()
    if (filters) previewCleanup.mutate(filters)
  }

  function requestExport(format: HistoryExportFormat) {
    const filters = buildFilters(status.data?.limits.export_records ?? 100_000)
    if (filters) exportHistory.mutate({ filters, format })
  }

  function requestRetentionSave() {
    if (!retentionPolicy) {
      setRetentionError(t('保留天数应为 1 至 36500，留空表示关闭'))
      return
    }
    const cleanTimezone = retentionTimezone.trim()
    const cleanHour = Number(retentionHour)
    if (!cleanTimezone || cleanTimezone.length > 128 || !Number.isInteger(cleanHour) || cleanHour < 0 || cleanHour > 23) {
      setRetentionError(t('自动执行时区或整点无效'))
      return
    }
    if (retentionAutoEnabled && !Object.values(retentionPolicy).some((value) => value !== null)) {
      setRetentionError(t('启用自动清理前至少设置一类保留天数'))
      return
    }
    setRetentionError('')
    saveRetention.mutate({
      policy: retentionPolicy,
      schedule: {
        auto_enabled: retentionAutoEnabled,
        timezone: cleanTimezone,
        hour: cleanHour,
      },
    })
  }

  if (status.isError && !status.data) {
    return (
      <EmptyState
        role="alert"
        title={t('历史服务暂不可用')}
        description={t('无法读取任务历史状态，请稍后重试。')}
        action={<Button type="button" onClick={() => void status.refetch()}>{t('重新加载')}</Button>}
      />
    )
  }

  return (
    <div className="history-maintenance">
      {status.isError ? <InlineNotice tone="warning" role="status">{t('历史状态暂时无法刷新，当前显示的是上次读取的数据。')}</InlineNotice> : null}
      {!status.data ? <SkeletonRows count={3} /> : (
        <>
          <section className="history-status-strip" aria-label={t('历史维护状态')}>
            <div>
              <span>{t('保留策略')}</span>
              <StatusBadge tone={retentionEnabled ? 'info' : 'neutral'}>{retentionEnabled ? t('已配置') : t('默认关闭')}</StatusBadge>
            </div>
            <div>
              <span>{t('自动清理')}</span>
              <StatusBadge tone={retentionAutomaticError ? 'danger' : retentionSchedule?.active ? 'success' : 'neutral'}>
                {retentionAutomaticError ? t('需要处理') : retentionSchedule?.active ? t('已启用') : t('未启用')}
              </StatusBadge>
            </div>
            <div>
              <span>{t('维护模式')}</span>
              <StatusBadge tone={status.data?.maintenance_mode ? 'warning' : 'neutral'}>
                {status.data?.maintenance_mode ? t('已启用') : t('未启用')}
              </StatusBadge>
            </div>
            <div>
              <span>{t('可恢复备份')}</span>
              <StatusBadge tone={status.data?.backup_verified ? 'success' : 'neutral'}>
                {status.data?.backup_verified ? t('已验证') : t('未验证')}
              </StatusBadge>
            </div>
          </section>

          <section className="history-workspace" aria-labelledby="history-filter-title">
            <div className="section-toolbar">
              <div>
                <h3 id="history-filter-title">{t('清理范围')}</h3>
                <span>{t('预览固定记录后才可执行')}</span>
              </div>
              <Archive aria-hidden="true" />
            </div>
            <form className="history-filter-form" onSubmit={submitPreview}>
              <fieldset className="history-type-selector">
                <legend>{t('任务类型')}</legend>
                {(Object.keys(taskTypeLabels) as HistoryTaskType[]).map((type) => (
                  <label key={type}>
                    <input
                      type="checkbox"
                      checked={types[type]}
                      onChange={(event) => {
                        setTypes((current) => ({ ...current, [type]: event.target.checked }))
                        clearPreview()
                      }}
                    />
                    <span>{taskTypeLabels[type]}</span>
                  </label>
                ))}
              </fieldset>
              <div className="history-filter-grid">
                {(Object.keys(taskTypeLabels) as HistoryTaskType[]).map((type) => (
                  <Field label={t('{value}状态', { value: taskTypeLabels[type] })} key={type}>
                    <select
                      value={statuses[type]}
                      disabled={!types[type]}
                      onChange={(event) => {
                        setStatuses((current) => ({ ...current, [type]: event.target.value }))
                        clearPreview()
                      }}
                    >
                      {statusOptions[type].map((option) => <option value={option.value} key={option.value}>{option.label}</option>)}
                    </select>
                  </Field>
                ))}
                <Field label={t('番号')}>
                  <div className="input-with-icon">
                    <Search aria-hidden="true" />
                    <input
                      value={code}
                      onChange={(event) => {
                        setCode(event.target.value)
                        clearPreview()
                      }}
                      placeholder={t('请输入番号')}
                      maxLength={40}
                    />
                  </div>
                </Field>
                <Field label={t('时间字段')}>
                  <select value={timeField} onChange={(event) => {
                    setTimeField(event.target.value as 'created' | 'updated')
                    clearPreview()
                  }}>
                    <option value="updated">{t('更新时间')}</option>
                    <option value="created">{t('创建时间')}</option>
                  </select>
                </Field>
                <Field label={t('起始时间')}>
                  <input type="datetime-local" value={timeAfter} onChange={(event) => {
                    setTimeAfter(event.target.value)
                    clearPreview()
                  }} />
                </Field>
                <Field label={t('截止时间')}>
                  <input type="datetime-local" value={timeBefore} onChange={(event) => {
                    setTimeBefore(event.target.value)
                    clearPreview()
                  }} />
                </Field>
                <Field label={t('记录上限')}>
                  <input
                    type="number"
                    min={1}
                    max={status.data?.limits.export_records ?? 100_000}
                    value={limit}
                    onChange={(event) => {
                      setLimit(event.target.value)
                      clearPreview()
                    }}
                  />
                </Field>
              </div>
              <div className="history-filter-actions">
                <span className="field-error" role={filterError ? 'alert' : undefined}>{filterError}</span>
                <div>
                  <Button type="submit" variant="primary" disabled={previewCleanup.isPending}>
                    <Search aria-hidden="true" />
                    {previewCleanup.isPending ? t('正在预览') : t('生成清理预览')}
                  </Button>
                  <Button type="button" onClick={() => requestExport('json')} disabled={exportHistory.isPending}>
                    <FileJson aria-hidden="true" />
                    {t('导出 JSON')}
                  </Button>
                  <Button type="button" onClick={() => requestExport('csv')} disabled={exportHistory.isPending}>
                    <FileSpreadsheet aria-hidden="true" />
                    {t('导出 CSV')}
                  </Button>
                </div>
              </div>
            </form>

            {lastExport ? (
              <div className="history-export-result" role="status">
                <Download aria-hidden="true" />
                <span>{lastExport.filename}</span>
                <code title={lastExport.checksum}>sha256:{lastExport.checksum}</code>
              </div>
            ) : null}

            {preview ? (
              <div className="history-preview" aria-live="polite">
                <div className="summary-strip" role="group" aria-label={t('清理预览摘要')}>
                  <div><span>{t('固定记录')}</span><strong>{preview.selected.records}</strong></div>
                  <div><span>{t('完整分组')}</span><strong>{preview.selected.groups}</strong></div>
                  <div><span>{t('估算空间')}</span><strong>{formatBytes(preview.selected.estimated_bytes)}</strong></div>
                  <div><span>{t('预览状态')}</span><strong>{previewExpired ? t('已过期') : t('{dateTime} 前有效', { dateTime: formatDateTime(preview.expires_at) })}</strong></div>
                </div>
                <SkipSummary skipped={preview.skipped} />
                <PreviewTable preview={preview} />
                {preview.selected.records > 0 ? (
                  confirmCleanup ? (
                    <div className="inline-confirm" role="alert">
                      <strong>{t('确认清理 {records} 条任务记录？', { records: preview.selected.records })}</strong>
                      <span>{t('只删除固定的任务历史，不删除媒体、NFO、图片或断点文件。状态已变化的记录会自动跳过。')}</span>
                      <div>
                        <Button
                          type="button"
                          variant="danger"
                          disabled={executeCleanup.isPending || previewExpired}
                          onClick={() => executeCleanup.mutate(preview.preview_token)}
                        >
                          <Trash2 aria-hidden="true" />
                          {executeCleanup.isPending ? t('正在清理') : t('确认清理记录')}
                        </Button>
                        <Button type="button" variant="ghost" onClick={() => setConfirmCleanup(false)}>{t('取消')}</Button>
                      </div>
                    </div>
                  ) : (
                    <div className="history-preview-actions">
                      <Button type="button" variant="danger" disabled={previewExpired} onClick={() => setConfirmCleanup(true)}>
                        <Trash2 aria-hidden="true" />
                        {t('准备清理')}
                      </Button>
                    </div>
                  )
                ) : null}
              </div>
            ) : null}

            {cleanupReport ? (
              <InlineNotice tone={cleanupReport.removed.records ? 'success' : 'warning'} role="status">
                {t('已清理 {records} 条记录，跳过 {value} 条。', { records: cleanupReport.removed.records, value: Object.values(cleanupReport.skipped.counts).reduce((sum, count) => sum + count, 0) })}
                {cleanupReport.vacuum_required ? t(' SQLite 空闲页可在维护模式中另行回收。') : ''}
              </InlineNotice>
            ) : null}
          </section>

          <div className="history-governance-grid">
            <section className="history-tool" aria-labelledby="history-retention-title">
              <div className="section-toolbar">
                <div>
                  <h3 id="history-retention-title">{t('保留策略')}</h3>
                  <span>{t('按任务类型设置天数')}</span>
                </div>
                <ShieldCheck aria-hidden="true" />
              </div>
              <div className="history-retention-grid">
                {(Object.keys(taskTypeLabels) as HistoryTaskType[]).map((type) => (
                  <Field label={t('{value}保留天数', { value: taskTypeLabels[type] })} key={type}>
                    <input
                      type="number"
                      min={1}
                      max={36_500}
                      placeholder={t('关闭')}
                      value={retention[type]}
                      onChange={(event) => {
                        setRetention((current) => ({ ...current, [type]: event.target.value }))
                        setRetentionDirty(true)
                        setRetentionError('')
                      }}
                    />
                  </Field>
                ))}
              </div>
              <div className="history-retention-schedule">
                <label className="history-retention-toggle">
                  <input
                    type="checkbox"
                    checked={retentionAutoEnabled}
                    onChange={(event) => {
                      setRetentionAutoEnabled(event.target.checked)
                      setRetentionDirty(true)
                      setRetentionError('')
                    }}
                  />
                  <span>{t('每日自动清理')}</span>
                </label>
                <Field label={t('IANA 时区')}>
                  <input
                    type="text"
                    list="history-retention-timezones"
                    value={retentionTimezone}
                    maxLength={128}
                    disabled={!retentionAutoEnabled}
                    onChange={(event) => {
                      setRetentionTimezone(event.target.value)
                      setRetentionDirty(true)
                      setRetentionError('')
                    }}
                  />
                  <datalist id="history-retention-timezones">
                    {retentionTimezones.map((timezone) => <option value={timezone} key={timezone} />)}
                  </datalist>
                </Field>
                <Field label={t('每日执行整点')}>
                  <select
                    value={retentionHour}
                    disabled={!retentionAutoEnabled}
                    onChange={(event) => {
                      setRetentionHour(event.target.value)
                      setRetentionDirty(true)
                      setRetentionError('')
                    }}
                  >
                    {Array.from({ length: 24 }, (_, hour) => (
                      <option value={hour} key={hour}>{hour.toString().padStart(2, '0')}:00</option>
                    ))}
                  </select>
                </Field>
              </div>
              <div className="history-retention-runtime" aria-live="polite">
                <span>
                  {t('最近运行：')}{retentionSchedule?.last_started_at ? formatDateTime(retentionSchedule.last_started_at) : t('尚未运行')}
                  {retentionSchedule?.last_removed_records ? t(' · 清理 {last_removed_records} 条', { last_removed_records: retentionSchedule.last_removed_records }) : ''}
                </span>
                <span>
                  {retentionAutomaticError
                    || (retentionSchedule?.active && retentionSchedule.next_run_at
                      ? t('下次运行：{dateTime}', { dateTime: formatDateTime(retentionSchedule.next_run_at) })
                      : t('自动清理当前关闭'))}
                </span>
              </div>
              {retentionRecoveryRequired ? (
                <InlineNotice tone="danger" role="alert">
                  <strong>{t('自动保留状态文件损坏')}</strong>
                  <span>
                    {retentionSchedule?.active
                      ? t('先关闭每日自动清理并保存策略，再恢复状态。')
                      : retentionDirty
                        ? t('先保存或撤销当前策略改动，再恢复状态。')
                        : t('可将损坏文件原样隔离保存，并重新创建空状态。')}
                  </span>
                  {!confirmRetentionRecovery ? (
                    <Button
                      type="button"
                      size="small"
                      className="history-retention-recovery-action"
                      disabled={!canRecoverRetentionState || recoverRetentionState.isPending}
                      onClick={() => setConfirmRetentionRecovery(true)}
                    >
                      <RefreshCw aria-hidden="true" />
                      {t('准备恢复状态')}
                    </Button>
                  ) : null}
                </InlineNotice>
              ) : null}
              {confirmRetentionRecovery && retentionRecoveryRequired ? (
                <div className="inline-confirm" role="alert">
                  <strong>{t('确认恢复自动保留状态？')}</strong>
                  <span>{t('损坏文件会保留为隔离副本；任务历史、下载记录和媒体文件不会被删除。')}</span>
                  <div>
                    <Button
                      type="button"
                      variant="danger"
                      disabled={!canRecoverRetentionState || recoverRetentionState.isPending}
                      onClick={() => recoverRetentionState.mutate()}
                    >
                      {recoverRetentionState.isPending ? t('正在恢复') : t('确认恢复状态')}
                    </Button>
                    <Button type="button" onClick={() => setConfirmRetentionRecovery(false)}>{t('取消')}</Button>
                  </div>
                </div>
              ) : null}
              {retentionRecoveryBackup ? (
                <InlineNotice tone="success" role="status">
                  {t('自动保留状态已恢复，原损坏文件已隔离为')} <code>{retentionRecoveryBackup}</code>{t('。')}
                </InlineNotice>
              ) : null}
              {retentionError ? <span className="field-error" role="alert">{retentionError}</span> : null}
              <div className="history-tool-actions">
                <Button type="button" variant="primary" disabled={!retentionDirty || saveRetention.isPending} onClick={requestRetentionSave}>
                  {saveRetention.isPending ? t('正在保存') : t('保存策略')}
                </Button>
                <Button
                  type="button"
                  disabled={retentionDirty || !retentionEnabled || previewRetention.isPending}
                  onClick={() => {
                    const maximum = status.data?.limits.cleanup_records ?? 10_000
                    const cleanLimit = Number(limit)
                    if (!Number.isInteger(cleanLimit) || cleanLimit < 1 || cleanLimit > maximum) {
                      setFilterError(t('记录上限应为 1 至 {maximum}', { maximum }))
                      return
                    }
                    setFilterError('')
                    previewRetention.mutate(cleanLimit)
                  }}
                >
                  <Search aria-hidden="true" />
                  {t('按策略预览')}
                </Button>
              </div>
            </section>

            <section className="history-tool" aria-labelledby="history-vacuum-title">
              <div className="section-toolbar">
                <div>
                  <h3 id="history-vacuum-title">{t('数据库空间回收')}</h3>
                  <span>{t('与记录清理分离执行')}</span>
                </div>
                <Database aria-hidden="true" />
              </div>
              <Field label={t('目标数据库')}>
                <select value={vacuumTarget} onChange={(event) => {
                  setVacuumTarget(event.target.value as HistoryVacuumTarget)
                  setConfirmVacuum(false)
                  setVacuumReport(null)
                }}>
                  {(Object.keys(vacuumLabels) as HistoryVacuumTarget[]).map((target) => (
                    <option value={target} key={target}>{vacuumLabels[target]}</option>
                  ))}
                </select>
              </Field>
              {!canVacuum ? (
                <InlineNotice tone="warning">
                  {t('VACUUM 仅在维护模式已启用且备份验证通过时可执行。')}
                </InlineNotice>
              ) : null}
              {confirmVacuum ? (
                <div className="inline-confirm" role="alert">
                  <strong>{t('确认回收{value}空闲页？', { value: vacuumLabels[vacuumTarget] })}</strong>
                  <span>{t('执行前后都会检查 SQLite 完整性。媒体文件与任务断点不在操作范围内。')}</span>
                  <div>
                    <Button type="button" variant="danger" disabled={vacuum.isPending} onClick={() => vacuum.mutate(vacuumTarget)}>
                      <Database aria-hidden="true" />
                      {vacuum.isPending ? t('正在回收') : t('确认 VACUUM')}
                    </Button>
                    <Button type="button" variant="ghost" onClick={() => setConfirmVacuum(false)}>{t('取消')}</Button>
                  </div>
                </div>
              ) : (
                <div className="history-tool-actions">
                  <Button type="button" disabled={!canVacuum} onClick={() => setConfirmVacuum(true)}>
                    <Database aria-hidden="true" />
                    {t('准备 VACUUM')}
                  </Button>
                </div>
              )}
              {vacuumReport ? (
                <InlineNotice tone="success" role="status">
                  {t('完整性检查通过，已回收 {bytes}。', { bytes: formatBytes(vacuumReport.reclaimed_bytes) })}
                </InlineNotice>
              ) : null}
            </section>
          </div>
        </>
      )}
    </div>
  )
}
