import { useMutation, useQueryClient } from '@tanstack/react-query'
import { Pause, Play, Plus, Save, Settings, Trash2 } from 'lucide-react'
import { useEffect, useState } from 'react'

import { useToast } from '../components/ToastProvider'
import { Button, IconButton, StatusBadge } from '../components/ui'
import { api } from '../lib/api'
import { formatBytes } from '../lib/format'
import type { WebDownloadControl, WebDownloadScheduleWindow } from '../types'

const scheduleDayLabels = ['一', '二', '三', '四', '五', '六', '日'] as const

type ScheduleDraftWindow = WebDownloadScheduleWindow & { draftId: number }

interface QueueControlDraft {
  targetConcurrency: number
  bandwidthMib: string
  timezone: string
  schedule: ScheduleDraftWindow[]
}

let nextScheduleDraftId = 0

function editableBandwidth(control: WebDownloadControl): string {
  return control.bandwidth_limit ? String(Math.round(control.bandwidth_limit / 1024 ** 2)) : '0'
}

function createControlDraft(control: WebDownloadControl): QueueControlDraft {
  return {
    targetConcurrency: control.target_concurrency,
    bandwidthMib: editableBandwidth(control),
    timezone: control.timezone,
    schedule: control.schedule.map((window) => ({
      days: [...window.days],
      start: window.start,
      end: window.end,
      draftId: ++nextScheduleDraftId,
    })),
  }
}

function controlDraftVersion({ targetConcurrency, bandwidthMib, timezone, schedule }: QueueControlDraft): string {
  return JSON.stringify({
    targetConcurrency,
    bandwidthMib,
    timezone,
    schedule: schedule.map(({ days, start, end }) => ({ days, start, end })),
  })
}

function serverControlVersion(control: WebDownloadControl): string {
  return JSON.stringify({
    targetConcurrency: control.target_concurrency,
    bandwidthMib: editableBandwidth(control),
    timezone: control.timezone,
    schedule: control.schedule.map(({ days, start, end }) => ({ days, start, end })),
  })
}

export function WebDownloadQueueControls({
  control,
  hardLimit,
  disabled,
}: {
  control: WebDownloadControl
  hardLimit: number
  disabled: boolean
}) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<QueueControlDraft>(() => createControlDraft(control))
  const [syncedServerVersion, setSyncedServerVersion] = useState(() => serverControlVersion(control))
  const draftVersion = controlDraftVersion(draft)
  const incomingServerVersion = serverControlVersion(control)
  const dirty = draftVersion !== syncedServerVersion

  const { targetConcurrency, bandwidthMib, timezone, schedule } = draft

  useEffect(() => {
    if (incomingServerVersion === syncedServerVersion || dirty) return
    setDraft(createControlDraft(control))
    setSyncedServerVersion(incomingServerVersion)
  }, [control, dirty, incomingServerVersion, syncedServerVersion])

  const update = useMutation({
    mutationFn: (payload: Partial<Pick<WebDownloadControl, 'target_concurrency' | 'bandwidth_limit' | 'timezone' | 'schedule'>>) => api.updateWebDownloadControl(payload),
    onSuccess: (payload) => {
      setDraft(createControlDraft(payload.control))
      setSyncedServerVersion(serverControlVersion(payload.control))
      toast.push('Web 下载队列设置已保存', 'success')
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const globalAction = useMutation({
    mutationFn: (action: 'pause' | 'resume') => api.webDownloadGlobalAction(action),
    onSuccess: (_payload, action) => {
      toast.push(action === 'pause' ? '正在暂停全部 Web 下载' : 'Web 下载队列已恢复', 'success')
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })

  function save() {
    if (!bandwidthMib.trim()) {
      toast.push('聚合限速不能为空；不限速请输入 0', 'error')
      return
    }
    const bandwidth = Number(bandwidthMib)
    if (!Number.isInteger(bandwidth) || bandwidth < 0 || bandwidth > 10 * 1024) {
      toast.push('聚合限速必须是 0 到 10240 MiB/s 的整数', 'error')
      return
    }
    if (
      !timezone.trim()
      || schedule.some((window) => (
        !window.days.length
        || !/^\d{2}:\d{2}$/.test(window.start)
        || !/^\d{2}:\d{2}$/.test(window.end)
        || window.start === window.end
      ))
    ) {
      toast.push('请完成时区和下载时段设置', 'error')
      return
    }
    update.mutate({
      target_concurrency: targetConcurrency,
      bandwidth_limit: bandwidth * 1024 ** 2,
      timezone: timezone.trim(),
      schedule: schedule.map(({ days, start, end }) => ({ days, start, end })),
    })
  }

  const savedScheduleLabel = control.schedule.length ? `${control.schedule.length} 个时段` : '始终开放'
  return (
    <section className="web-queue-controls" aria-labelledby="web-queue-controls-title">
      <header className="web-queue-controls-header">
        <div>
          <Settings aria-hidden="true" />
          <div>
            <h2 id="web-queue-controls-title">队列控制</h2>
            <span>并发 {control.target_concurrency} · {control.bandwidth_limit ? `${formatBytes(control.bandwidth_limit, true)}` : '不限速'} · {savedScheduleLabel}</span>
          </div>
        </div>
        <Button
          type="button"
          size="small"
          onClick={() => globalAction.mutate(control.global_paused ? 'resume' : 'pause')}
          disabled={disabled || globalAction.isPending}
        >
          {control.global_paused ? <Play aria-hidden="true" /> : <Pause aria-hidden="true" />}
          {control.global_paused ? '恢复全部' : '暂停全部'}
        </Button>
      </header>
      <details className="web-queue-control-details">
        <summary>
          调度设置
          {control.global_paused ? <StatusBadge tone="warning">全局已暂停</StatusBadge> : null}
          {dirty ? <StatusBadge tone="info">有未保存更改</StatusBadge> : null}
        </summary>
        <div className="web-queue-control-grid">
          <label>
            <span>运行并发</span>
            <select value={targetConcurrency} onChange={(event) => setDraft((current) => ({ ...current, targetConcurrency: Number(event.target.value) }))} disabled={disabled || update.isPending}>
              {Array.from({ length: Math.max(1, hardLimit) }, (_item, index) => index + 1).map((value) => (
                <option value={value} key={value}>{value}</option>
              ))}
            </select>
          </label>
          <label>
            <span>聚合限速 (MiB/s)</span>
            <input type="number" min="0" max="10240" step="1" value={bandwidthMib} onChange={(event) => setDraft((current) => ({ ...current, bandwidthMib: event.target.value }))} disabled={disabled || update.isPending} />
          </label>
          <label>
            <span>时区</span>
            <input value={timezone} maxLength={64} list="web-download-timezones" onChange={(event) => setDraft((current) => ({ ...current, timezone: event.target.value }))} disabled={disabled || update.isPending} />
            <datalist id="web-download-timezones">
              <option value="Asia/Shanghai" />
              <option value="UTC" />
              <option value="Asia/Tokyo" />
              <option value="America/New_York" />
              <option value="Europe/Berlin" />
            </datalist>
          </label>
        </div>
        <div className="web-queue-schedule-toolbar">
          <strong>下载时段</strong>
          <Button
            type="button"
            size="small"
            variant="ghost"
            disabled={disabled || update.isPending || schedule.length >= 32}
            onClick={() => setDraft((current) => ({
              ...current,
              schedule: [...current.schedule, {
                days: [0, 1, 2, 3, 4, 5, 6],
                start: '00:00',
                end: '06:00',
                draftId: ++nextScheduleDraftId,
              }],
            }))}
          >
            <Plus aria-hidden="true" />
            添加时段
          </Button>
        </div>
        <div className="web-queue-schedule-list">
          {!schedule.length ? <span className="web-queue-schedule-empty">未设置时段限制</span> : null}
          {schedule.map((window, index) => (
            <div className="web-queue-schedule-row" key={window.draftId}>
              <fieldset>
                <legend>星期</legend>
                {scheduleDayLabels.map((label, day) => (
                  <label key={label}>
                    <input
                      type="checkbox"
                      checked={window.days.includes(day)}
                      disabled={disabled || update.isPending}
                      onChange={(event) => setDraft((current) => ({
                        ...current,
                        schedule: current.schedule.map((item, itemIndex) => itemIndex !== index ? item : {
                          ...item,
                          days: event.target.checked
                            ? [...item.days, day].sort((left, right) => left - right)
                            : item.days.filter((value) => value !== day),
                        }),
                      }))}
                    />
                    {label}
                  </label>
                ))}
              </fieldset>
              <label><span>开始</span><input type="time" value={window.start} disabled={disabled || update.isPending} onChange={(event) => setDraft((current) => ({ ...current, schedule: current.schedule.map((item, itemIndex) => itemIndex === index ? { ...item, start: event.target.value } : item) }))} /></label>
              <label><span>结束</span><input type="time" value={window.end} disabled={disabled || update.isPending} onChange={(event) => setDraft((current) => ({ ...current, schedule: current.schedule.map((item, itemIndex) => itemIndex === index ? { ...item, end: event.target.value } : item) }))} /></label>
              <IconButton label={`删除时段 ${index + 1}`} size="small" className="danger-icon" onClick={() => setDraft((current) => ({ ...current, schedule: current.schedule.filter((_item, itemIndex) => itemIndex !== index) }))} disabled={disabled || update.isPending}>
                <Trash2 aria-hidden="true" />
              </IconButton>
            </div>
          ))}
        </div>
        <div className="web-queue-control-actions">
          <Button type="button" size="small" variant="primary" onClick={save} disabled={disabled || update.isPending || !dirty}>
            <Save aria-hidden="true" />
            {update.isPending ? '保存中' : '保存调度'}
          </Button>
        </div>
      </details>
    </section>
  )
}
