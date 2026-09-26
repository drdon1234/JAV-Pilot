import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { BellRing, RefreshCw, RotateCcw, Save, Send } from 'lucide-react'
import { useEffect, useId, useMemo, useState } from 'react'

import '../styles/notifications.css'

import { useToast } from '../components/ToastProvider'
import { Button, Field, InlineNotice, SkeletonRows, StatusBadge, Toggle } from '../components/ui'
import { api } from '../lib/api'
import { diagnosticStageLabel, errorCodeLabel } from '../lib/presentation'
import type {
  NotificationChannel,
  NotificationChannelPublicConfig,
  NotificationConfigUpdate,
  NotificationDeliveryOutcome,
  NotificationDeliveryState,
  NotificationEvent,
  NotificationEventType,
  NotificationPublicConfig,
} from '../types'

interface ChannelDraft {
  enabled: boolean
  target: string
  secret: string
  secondarySecret: string
  apiOrigin: string
  privateOrigin: string
  pins: string
  priority: number
  clearTarget: boolean
  clearSecret: boolean
  clearSecondarySecret: boolean
  clearApiOrigin: boolean
  clearPrivateOrigin: boolean
  clearPins: boolean
}

type NotificationDraft = Record<NotificationChannel, ChannelDraft>

const channelLabels: Record<NotificationChannel, string> = {
  webhook: 'Webhook',
  gotify: 'Gotify',
  telegram: 'Telegram',
  nas: 'NAS 通知',
}

const eventLabels: Record<NotificationEventType, string> = {
  completed: '任务完成',
  failed: '任务失败',
  disk_low: '磁盘空间不足',
  site_failure: '站点连续失败',
  test: '测试事件',
}

const deliveryLabels: Record<NotificationDeliveryState, string> = {
  pending: '等待投递',
  inflight: '正在投递',
  delivered: '已送达',
  dead: '投递失败',
}

const outcomeLabels: Record<NotificationDeliveryOutcome, string> = {
  delivered: '已送达',
  retry: '等待重试',
  dead: '停止重试',
  lease_expired: '调度中断',
  manual_retry: '手动重试',
}

const eventStatusLabels: Record<string, string> = {
  completed: '已完成',
  dead: '投递失败',
  failed: '失败',
  pending: '等待处理',
  running: '处理中',
  site_failure: '站点异常',
  test: '测试',
}

const siteLabels: Record<string, string> = {
  jable: 'JableTV',
  javbus: 'JavBus',
  javdb: 'JavDB',
  missav: 'MissAV',
  supjav: 'SupJav',
}

const eventSourceLabels: Record<string, string> = {
  disk: '磁盘监控',
  manual: '手动测试',
  site: '站点诊断',
}

const timestampFormatter = new Intl.DateTimeFormat('zh-CN', {
  month: '2-digit',
  day: '2-digit',
  hour: '2-digit',
  minute: '2-digit',
  second: '2-digit',
  hour12: false,
})

function emptyChannel(enabled = false, priority = 5): ChannelDraft {
  return {
    enabled,
    target: '',
    secret: '',
    secondarySecret: '',
    apiOrigin: '',
    privateOrigin: '',
    pins: '',
    priority,
    clearTarget: false,
    clearSecret: false,
    clearSecondarySecret: false,
    clearApiOrigin: false,
    clearPrivateOrigin: false,
    clearPins: false,
  }
}

function draftFromConfig(config?: NotificationPublicConfig): NotificationDraft {
  return {
    webhook: emptyChannel(config?.webhook.enabled),
    gotify: emptyChannel(config?.gotify.enabled, config?.gotify.priority ?? 5),
    telegram: emptyChannel(config?.telegram.enabled),
    nas: emptyChannel(config?.nas.enabled),
  }
}

function parsePins(value: string): string[] {
  return Array.from(new Set(value.split(/[\s,]+/).map((item) => item.trim()).filter(Boolean)))
}

function buildConfigUpdate(draft: NotificationDraft): NotificationConfigUpdate {
  const webhook: NonNullable<NotificationConfigUpdate['webhook']> = { enabled: draft.webhook.enabled }
  const webhookClear: NonNullable<NotificationConfigUpdate['webhook']>['clear_fields'] = []
  if (draft.webhook.target.trim()) webhook.endpoint = draft.webhook.target.trim()
  if (draft.webhook.secret) webhook.signing_secret = draft.webhook.secret
  if (draft.webhook.privateOrigin.trim()) webhook.private_origin = draft.webhook.privateOrigin.trim()
  if (draft.webhook.pins.trim()) webhook.pinned_addresses = parsePins(draft.webhook.pins)
  if (draft.webhook.clearTarget) webhookClear.push('endpoint')
  if (draft.webhook.clearSecret) webhookClear.push('signing_secret')
  if (draft.webhook.clearPrivateOrigin) webhookClear.push('private_origin')
  if (draft.webhook.clearPins) webhookClear.push('pinned_addresses')
  if (webhookClear.length) webhook.clear_fields = webhookClear

  const gotify: NonNullable<NotificationConfigUpdate['gotify']> = {
    enabled: draft.gotify.enabled,
    priority: draft.gotify.priority,
  }
  const gotifyClear: NonNullable<NotificationConfigUpdate['gotify']>['clear_fields'] = []
  if (draft.gotify.target.trim()) gotify.origin = draft.gotify.target.trim()
  if (draft.gotify.secret) gotify.app_token = draft.gotify.secret
  if (draft.gotify.privateOrigin.trim()) gotify.private_origin = draft.gotify.privateOrigin.trim()
  if (draft.gotify.pins.trim()) gotify.pinned_addresses = parsePins(draft.gotify.pins)
  if (draft.gotify.clearTarget) gotifyClear.push('origin')
  if (draft.gotify.clearSecret) gotifyClear.push('app_token')
  if (draft.gotify.clearPrivateOrigin) gotifyClear.push('private_origin')
  if (draft.gotify.clearPins) gotifyClear.push('pinned_addresses')
  if (gotifyClear.length) gotify.clear_fields = gotifyClear

  const telegram: NonNullable<NotificationConfigUpdate['telegram']> = { enabled: draft.telegram.enabled }
  const telegramClear: NonNullable<NotificationConfigUpdate['telegram']>['clear_fields'] = []
  if (draft.telegram.secret) telegram.bot_token = draft.telegram.secret
  if (draft.telegram.secondarySecret) telegram.chat_id = draft.telegram.secondarySecret
  if (draft.telegram.apiOrigin.trim()) telegram.api_origin = draft.telegram.apiOrigin.trim()
  if (draft.telegram.privateOrigin.trim()) telegram.private_origin = draft.telegram.privateOrigin.trim()
  if (draft.telegram.pins.trim()) telegram.pinned_addresses = parsePins(draft.telegram.pins)
  if (draft.telegram.clearSecret) telegramClear.push('bot_token')
  if (draft.telegram.clearSecondarySecret) telegramClear.push('chat_id')
  if (draft.telegram.clearApiOrigin) telegramClear.push('api_origin')
  if (draft.telegram.clearPrivateOrigin) telegramClear.push('private_origin')
  if (draft.telegram.clearPins) telegramClear.push('pinned_addresses')
  if (telegramClear.length) telegram.clear_fields = telegramClear

  const nas: NonNullable<NotificationConfigUpdate['nas']> = { enabled: draft.nas.enabled }
  const nasClear: NonNullable<NotificationConfigUpdate['nas']>['clear_fields'] = []
  if (draft.nas.target.trim()) nas.endpoint = draft.nas.target.trim()
  if (draft.nas.secret) nas.signing_secret = draft.nas.secret
  if (draft.nas.privateOrigin.trim()) nas.private_origin = draft.nas.privateOrigin.trim()
  if (draft.nas.pins.trim()) nas.pinned_addresses = parsePins(draft.nas.pins)
  if (draft.nas.clearTarget) nasClear.push('endpoint')
  if (draft.nas.clearSecret) nasClear.push('signing_secret')
  if (draft.nas.clearPrivateOrigin) nasClear.push('private_origin')
  if (draft.nas.clearPins) nasClear.push('pinned_addresses')
  if (nasClear.length) nas.clear_fields = nasClear

  return { webhook, gotify, telegram, nas }
}

function formatTimestamp(value?: number | null): string {
  if (value === undefined || value === null || !Number.isFinite(value)) return '尚无记录'
  return timestampFormatter.format(new Date(value * 1000))
}

function eventType(event: NotificationEvent): NotificationEventType {
  return event.type || event.event_type || 'failed'
}

function eventTimestamp(event: NotificationEvent): number | undefined {
  return event.last_occurred_at ?? event.occurred_at ?? event.created_at
}

function eventSubject(event: NotificationEvent): string {
  const subjectId = event.subject_id?.trim() || ''
  const subject = event.subject_kind === 'site'
    ? siteLabels[subjectId] || subjectId
    : subjectId
  const stage = event.stage?.trim() ? diagnosticStageLabel(event.stage, '其他阶段') : ''
  return [subject, stage].filter(Boolean).join(' · ')
    || event.code
    || eventSourceLabels[event.source]
    || '系统事件'
}

function eventStatusLabel(status: string): string {
  return eventStatusLabels[status] || '状态未知'
}

function deliveryTone(state: NotificationDeliveryState): 'neutral' | 'info' | 'success' | 'danger' {
  if (state === 'delivered') return 'success'
  if (state === 'dead') return 'danger'
  if (state === 'inflight') return 'info'
  return 'neutral'
}

function eventTone(type: NotificationEventType): 'neutral' | 'success' | 'warning' | 'danger' | 'info' {
  if (type === 'completed') return 'success'
  if (type === 'failed' || type === 'site_failure') return 'danger'
  if (type === 'disk_low') return 'warning'
  if (type === 'test') return 'info'
  return 'neutral'
}

function WriteOnlyField({
  label,
  value,
  configured,
  clear,
  clearLabel,
  type = 'text',
  placeholder,
  detail,
  onValueChange,
  onClearChange,
}: {
  label: string
  value: string
  configured: boolean
  clear: boolean
  clearLabel: string
  type?: 'text' | 'url' | 'password'
  placeholder?: string
  detail?: string
  onValueChange: (value: string) => void
  onClearChange: (clear: boolean) => void
}) {
  const inputId = useId()
  const hintId = `${inputId}-hint`
  const configuredHint = configured ? '已配置；出于安全不会回显，留空保持不变。' : '仅写入保存，不会回显。'
  return (
    <div className="field">
      <label className="field-label" htmlFor={inputId}>{label}</label>
      <input
        id={inputId}
        type={type}
        value={value}
        placeholder={placeholder}
        disabled={clear}
        autoComplete={type === 'password' ? 'new-password' : 'off'}
        aria-describedby={hintId}
        onChange={(event) => onValueChange(event.target.value)}
      />
      <span className="field-hint" id={hintId}>{detail ? `${configuredHint}${detail}` : configuredHint}</span>
      {configured ? (
        <label className="notification-clear-control">
          <input
            type="checkbox"
            checked={clear}
            onChange={(event) => onClearChange(event.target.checked)}
          />
          <span>{clearLabel}</span>
        </label>
      ) : null}
    </div>
  )
}

function ChannelHeader({
  channel,
  config,
  enabled,
  onEnabledChange,
}: {
  channel: NotificationChannel
  config: NotificationChannelPublicConfig
  enabled: boolean
  onEnabledChange: (enabled: boolean) => void
}) {
  return (
    <div className="notification-channel-header">
      <div>
        <strong>{channelLabels[channel]}</strong>
        <span>{config.target_configured ? '目标已配置且不会回显' : '目标未配置'}</span>
      </div>
      <div className="notification-channel-status">
        {config.credential_configured ? <StatusBadge tone="success">凭据已配置</StatusBadge> : null}
        {config.private_target ? <StatusBadge tone="warning">私网固定</StatusBadge> : null}
        {config.pinned_address_count ? <StatusBadge tone="neutral">{config.pinned_address_count} 个地址固定</StatusBadge> : null}
        <Toggle label={`启用 ${channelLabels[channel]}`} checked={enabled} onChange={(event) => onEnabledChange(event.target.checked)} />
      </div>
    </div>
  )
}

export function NotificationSettingsPanel({ active }: { active: boolean }) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [draft, setDraft] = useState<NotificationDraft>(() => draftFromConfig())
  const [dirty, setDirty] = useState(false)
  const [selectedEventId, setSelectedEventId] = useState<string | null>(null)

  const notifications = useQuery({
    queryKey: ['notifications'],
    queryFn: () => api.notifications(30),
    enabled: active,
    refetchInterval: active ? 15_000 : false,
  })
  const detail = useQuery({
    queryKey: ['notification', selectedEventId],
    queryFn: () => api.notification(selectedEventId!),
    enabled: active && Boolean(selectedEventId),
    refetchInterval: active && selectedEventId ? 3_000 : false,
  })

  useEffect(() => {
    if (notifications.data?.config && !dirty) setDraft(draftFromConfig(notifications.data.config))
  }, [dirty, notifications.data?.config])

  useEffect(() => {
    const events = notifications.data?.events || []
    if (!events.length) return
    if (!selectedEventId) {
      setSelectedEventId(events[0].event_id)
    }
  }, [notifications.data?.events, selectedEventId])

  const enabledCount = useMemo(
    () => Object.values(draft).filter((channel) => channel.enabled).length,
    [draft],
  )

  function updateChannel(channel: NotificationChannel, update: Partial<ChannelDraft>) {
    setDirty(true)
    setDraft((current) => ({
      ...current,
      [channel]: { ...current[channel], ...update },
    }))
  }

  const saveConfig = useMutation({
    mutationFn: () => api.saveNotificationConfig(buildConfigUpdate(draft)),
    onSuccess: (value) => {
      setDraft(draftFromConfig(value.config))
      setDirty(false)
      queryClient.setQueryData(['notifications'], (current: typeof notifications.data) =>
        current ? { ...current, config: value.config } : current,
      )
      toast.push('通知设置已保存', 'success')
      void queryClient.invalidateQueries({ queryKey: ['notifications'] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const sendTest = useMutation({
    mutationFn: api.sendTestNotification,
    onSuccess: (value) => {
      setSelectedEventId(value.event_id)
      toast.push(value.created ? '测试事件已加入投递队列' : '测试事件已存在', 'success')
      void queryClient.invalidateQueries({ queryKey: ['notifications'] })
      void queryClient.invalidateQueries({ queryKey: ['notification', value.event_id] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const retryDelivery = useMutation({
    mutationFn: ({ eventId, adapter }: { eventId: string; adapter?: NotificationChannel }) =>
      api.retryNotificationDelivery(eventId, adapter),
    onSuccess: (value) => {
      toast.push(value.retried ? `已重新排队 ${value.retried} 个投递` : '没有可重试的失败投递', value.retried ? 'success' : 'info')
      void queryClient.invalidateQueries({ queryKey: ['notification', selectedEventId] })
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })

  if (!active) return null
  if (notifications.isLoading) return <SkeletonRows count={5} />
  if (notifications.isError) {
    return (
      <InlineNotice tone="danger">
        {(notifications.error as Error).message}
        <Button size="small" variant="ghost" onClick={() => void notifications.refetch()}>重新加载</Button>
      </InlineNotice>
    )
  }

  const notificationPayload = notifications.data
  if (!notificationPayload?.config) return <InlineNotice tone="danger">通知配置不可用</InlineNotice>
  const config = notificationPayload.config
  const savedEnabledCount = Object.values(config).filter((channel) => channel.enabled).length

  const selectedEvent = notificationPayload.events.find((event) => event.event_id === selectedEventId) || detail.data?.event
  const deadDeliveries = detail.data?.deliveries.filter((delivery) => delivery.state === 'dead') || []

  return (
    <div className="notification-settings">
      <div className="notification-actions">
        <span>{enabledCount ? `${enabledCount} 个通道开启` : '所有通道默认关闭'}</span>
        <Button
          size="small"
          variant="ghost"
          onClick={() => sendTest.mutate()}
          disabled={sendTest.isPending || savedEnabledCount === 0}
          title={savedEnabledCount ? '创建测试事件' : '请先启用并保存至少一个通道'}
        >
          <Send aria-hidden="true" />
          {sendTest.isPending ? '发送中' : '发送测试'}
        </Button>
      </div>

      <InlineNotice tone="info">
        私网目标必须填写与目标完全一致的 Origin，并固定当前解析出的全部 IP 地址；公网目标必须使用 HTTPS。
      </InlineNotice>

      <form
        className="notification-config-form"
        onSubmit={(event) => {
          event.preventDefault()
          saveConfig.mutate()
        }}
      >
        <fieldset className="notification-channel">
          <legend className="sr-only">Webhook 设置</legend>
          <ChannelHeader channel="webhook" config={config.webhook} enabled={draft.webhook.enabled} onEnabledChange={(enabled) => updateChannel('webhook', { enabled })} />
          <div className="notification-field-grid">
            <WriteOnlyField
              label="Webhook 地址"
              type="url"
              value={draft.webhook.target}
              configured={config.webhook.target_configured}
              clear={draft.webhook.clearTarget}
              clearLabel="清除已保存的地址"
              placeholder="https://notify.example.com/webhook"
              onValueChange={(target) => updateChannel('webhook', { target, clearTarget: false })}
              onClearChange={(clearTarget) => updateChannel('webhook', { clearTarget, target: '' })}
            />
            <WriteOnlyField
              label="签名密钥"
              type="password"
              value={draft.webhook.secret}
              configured={config.webhook.credential_configured}
              clear={draft.webhook.clearSecret}
              clearLabel="清除已保存的签名密钥"
              onValueChange={(secret) => updateChannel('webhook', { secret, clearSecret: false })}
              onClearChange={(clearSecret) => updateChannel('webhook', { clearSecret, secret: '' })}
            />
            <WriteOnlyField
              label="私网固定 Origin"
              type="url"
              value={draft.webhook.privateOrigin}
              configured={config.webhook.private_target}
              clear={draft.webhook.clearPrivateOrigin}
              clearLabel="清除私网 Origin"
              placeholder="http://192.0.2.10:8080"
              onValueChange={(privateOrigin) => updateChannel('webhook', { privateOrigin, clearPrivateOrigin: false })}
              onClearChange={(clearPrivateOrigin) => updateChannel('webhook', { clearPrivateOrigin, privateOrigin: '' })}
            />
            <WriteOnlyField
              label="固定 IP 地址"
              value={draft.webhook.pins}
              configured={config.webhook.pinned_address_count > 0}
              clear={draft.webhook.clearPins}
              clearLabel="清除固定地址"
              placeholder="192.0.2.10, 192.0.2.11"
              onValueChange={(pins) => updateChannel('webhook', { pins, clearPins: false })}
              onClearChange={(clearPins) => updateChannel('webhook', { clearPins, pins: '' })}
            />
          </div>
        </fieldset>

        <fieldset className="notification-channel">
          <legend className="sr-only">Gotify 设置</legend>
          <ChannelHeader channel="gotify" config={config.gotify} enabled={draft.gotify.enabled} onEnabledChange={(enabled) => updateChannel('gotify', { enabled })} />
          <div className="notification-field-grid">
            <WriteOnlyField
              label="Gotify Origin"
              type="url"
              value={draft.gotify.target}
              configured={config.gotify.target_configured}
              clear={draft.gotify.clearTarget}
              clearLabel="清除已保存的 Origin"
              placeholder="https://gotify.example.com"
              onValueChange={(target) => updateChannel('gotify', { target, clearTarget: false })}
              onClearChange={(clearTarget) => updateChannel('gotify', { clearTarget, target: '' })}
            />
            <WriteOnlyField
              label="应用 Token"
              type="password"
              value={draft.gotify.secret}
              configured={config.gotify.credential_configured}
              clear={draft.gotify.clearSecret}
              clearLabel="清除已保存的 Token"
              onValueChange={(secret) => updateChannel('gotify', { secret, clearSecret: false })}
              onClearChange={(clearSecret) => updateChannel('gotify', { clearSecret, secret: '' })}
            />
            <WriteOnlyField
              label="私网固定 Origin"
              type="url"
              value={draft.gotify.privateOrigin}
              configured={config.gotify.private_target}
              clear={draft.gotify.clearPrivateOrigin}
              clearLabel="清除私网 Origin"
              placeholder="http://192.0.2.10:8080"
              onValueChange={(privateOrigin) => updateChannel('gotify', { privateOrigin, clearPrivateOrigin: false })}
              onClearChange={(clearPrivateOrigin) => updateChannel('gotify', { clearPrivateOrigin, privateOrigin: '' })}
            />
            <WriteOnlyField
              label="固定 IP 地址"
              value={draft.gotify.pins}
              configured={config.gotify.pinned_address_count > 0}
              clear={draft.gotify.clearPins}
              clearLabel="清除固定地址"
              placeholder="192.0.2.10"
              onValueChange={(pins) => updateChannel('gotify', { pins, clearPins: false })}
              onClearChange={(clearPins) => updateChannel('gotify', { clearPins, pins: '' })}
            />
            <Field label="消息优先级" hint="Gotify 支持 -10 到 10。">
              <input
                type="number"
                min={-10}
                max={10}
                value={draft.gotify.priority}
                onChange={(event) => updateChannel('gotify', { priority: Number(event.target.value) })}
              />
            </Field>
          </div>
        </fieldset>

        <fieldset className="notification-channel">
          <legend className="sr-only">Telegram 设置</legend>
          <ChannelHeader channel="telegram" config={config.telegram} enabled={draft.telegram.enabled} onEnabledChange={(enabled) => updateChannel('telegram', { enabled })} />
          <div className="notification-field-grid">
            <WriteOnlyField
              label="Bot Token"
              type="password"
              value={draft.telegram.secret}
              configured={config.telegram.credential_configured}
              clear={draft.telegram.clearSecret}
              clearLabel="清除已保存的 Bot Token"
              onValueChange={(secret) => updateChannel('telegram', { secret, clearSecret: false })}
              onClearChange={(clearSecret) => updateChannel('telegram', { clearSecret, secret: '' })}
            />
            <WriteOnlyField
              label="Chat ID"
              type="password"
              value={draft.telegram.secondarySecret}
              configured={config.telegram.credential_configured}
              clear={draft.telegram.clearSecondarySecret}
              clearLabel="清除已保存的 Chat ID"
              onValueChange={(secondarySecret) => updateChannel('telegram', { secondarySecret, clearSecondarySecret: false })}
              onClearChange={(clearSecondarySecret) => updateChannel('telegram', { clearSecondarySecret, secondarySecret: '' })}
            />
            <WriteOnlyField
              label="API Origin"
              type="url"
              value={draft.telegram.apiOrigin}
              configured={config.telegram.target_configured}
              clear={draft.telegram.clearApiOrigin}
              clearLabel="清除自定义 API Origin"
              placeholder="https://api.telegram.org"
              onValueChange={(apiOrigin) => updateChannel('telegram', { apiOrigin, clearApiOrigin: false })}
              onClearChange={(clearApiOrigin) => updateChannel('telegram', { clearApiOrigin, apiOrigin: '' })}
            />
            <WriteOnlyField
              label="私网固定 Origin"
              type="url"
              value={draft.telegram.privateOrigin}
              configured={config.telegram.private_target}
              clear={draft.telegram.clearPrivateOrigin}
              clearLabel="清除私网 Origin"
              placeholder="http://192.0.2.10:8080"
              onValueChange={(privateOrigin) => updateChannel('telegram', { privateOrigin, clearPrivateOrigin: false })}
              onClearChange={(clearPrivateOrigin) => updateChannel('telegram', { clearPrivateOrigin, privateOrigin: '' })}
            />
            <WriteOnlyField
              label="固定 IP 地址"
              value={draft.telegram.pins}
              configured={config.telegram.pinned_address_count > 0}
              clear={draft.telegram.clearPins}
              clearLabel="清除固定地址"
              placeholder="192.0.2.10"
              onValueChange={(pins) => updateChannel('telegram', { pins, clearPins: false })}
              onClearChange={(clearPins) => updateChannel('telegram', { clearPins, pins: '' })}
            />
          </div>
        </fieldset>

        <fieldset className="notification-channel">
          <legend className="sr-only">NAS 通知设置</legend>
          <ChannelHeader channel="nas" config={config.nas} enabled={draft.nas.enabled} onEnabledChange={(enabled) => updateChannel('nas', { enabled })} />
          <div className="notification-field-grid">
            <WriteOnlyField
              label="NAS 通知地址"
              type="url"
              value={draft.nas.target}
              configured={config.nas.target_configured}
              clear={draft.nas.clearTarget}
              clearLabel="清除已保存的地址"
              placeholder="https://nas.example.com/notify"
              onValueChange={(target) => updateChannel('nas', { target, clearTarget: false })}
              onClearChange={(clearTarget) => updateChannel('nas', { clearTarget, target: '' })}
            />
            <WriteOnlyField
              label="签名密钥"
              type="password"
              value={draft.nas.secret}
              configured={config.nas.credential_configured}
              clear={draft.nas.clearSecret}
              clearLabel="清除已保存的签名密钥"
              onValueChange={(secret) => updateChannel('nas', { secret, clearSecret: false })}
              onClearChange={(clearSecret) => updateChannel('nas', { clearSecret, secret: '' })}
            />
            <WriteOnlyField
              label="私网固定 Origin"
              type="url"
              value={draft.nas.privateOrigin}
              configured={config.nas.private_target}
              clear={draft.nas.clearPrivateOrigin}
              clearLabel="清除私网 Origin"
              placeholder="http://192.0.2.10:8080"
              onValueChange={(privateOrigin) => updateChannel('nas', { privateOrigin, clearPrivateOrigin: false })}
              onClearChange={(clearPrivateOrigin) => updateChannel('nas', { clearPrivateOrigin, privateOrigin: '' })}
            />
            <WriteOnlyField
              label="固定 IP 地址"
              value={draft.nas.pins}
              configured={config.nas.pinned_address_count > 0}
              clear={draft.nas.clearPins}
              clearLabel="清除固定地址"
              placeholder="192.0.2.10"
              onValueChange={(pins) => updateChannel('nas', { pins, clearPins: false })}
              onClearChange={(clearPins) => updateChannel('nas', { clearPins, pins: '' })}
            />
          </div>
        </fieldset>

        <div className="form-footer notification-save-row">
          <span>留空会保持已保存值，只有勾选清除项才会删除。</span>
          <Button type="submit" variant="primary" disabled={saveConfig.isPending || !dirty}>
            <Save aria-hidden="true" />
            {saveConfig.isPending ? '保存中' : '保存通知设置'}
          </Button>
        </div>
      </form>

      <section className="notification-history" aria-labelledby="notification-history-title">
        <div className="notification-history-header">
          <div>
            <BellRing aria-hidden="true" />
            <div>
              <h3 id="notification-history-title">投递记录</h3>
              <span>仅显示事件、通道和脱敏错误码</span>
            </div>
          </div>
          <Button size="small" variant="ghost" onClick={() => void notifications.refetch()} disabled={notifications.isFetching}>
            <RefreshCw className={notifications.isFetching ? 'spin' : ''} aria-hidden="true" />
            刷新
          </Button>
        </div>

        {notificationPayload.events.length ? (
          <div className="notification-history-layout">
            <div className="notification-event-list" aria-label="通知事件">
              {notificationPayload.events.map((event) => {
                const type = eventType(event)
                return (
                  <button
                    type="button"
                    key={event.event_id}
                    className={event.event_id === selectedEventId ? 'is-selected' : ''}
                    aria-pressed={event.event_id === selectedEventId}
                    onClick={() => setSelectedEventId(event.event_id)}
                  >
                    <span>
                      <strong>{eventLabels[type]}</strong>
                      <StatusBadge tone={eventTone(type)}>{eventStatusLabel(event.status)}</StatusBadge>
                    </span>
                    <small>{eventSubject(event)} · {formatTimestamp(eventTimestamp(event))}</small>
                  </button>
                )
              })}
            </div>

            <div className="notification-event-detail" aria-live="polite">
              {detail.isLoading ? <SkeletonRows count={3} /> : detail.isError ? (
                <InlineNotice tone="danger">{(detail.error as Error).message}</InlineNotice>
              ) : detail.data && selectedEvent ? (
                <>
                  <div className="notification-event-summary">
                    <div>
                      <strong>{eventLabels[eventType(selectedEvent)]}</strong>
                      <span>{eventSubject(selectedEvent)} · {formatTimestamp(eventTimestamp(selectedEvent))}</span>
                    </div>
                    {deadDeliveries.length ? (
                      <Button
                        size="small"
                        variant="ghost"
                        onClick={() => retryDelivery.mutate({ eventId: selectedEvent.event_id })}
                        disabled={retryDelivery.isPending}
                      >
                        <RotateCcw aria-hidden="true" />
                        重试全部失败通道
                      </Button>
                    ) : null}
                  </div>

                  {detail.data.deliveries.length ? (
                    <div className="notification-delivery-list">
                      {detail.data.deliveries.map((delivery) => (
                        <div key={delivery.adapter}>
                          <div>
                            <strong>{channelLabels[delivery.adapter]}</strong>
                            <span>{delivery.attempt_count} 次尝试{delivery.last_http_status ? ` · HTTP ${delivery.last_http_status}` : ''}</span>
                          </div>
                          <StatusBadge tone={deliveryTone(delivery.state)}>{deliveryLabels[delivery.state]}</StatusBadge>
                          {delivery.state === 'dead' ? (
                            <Button
                              size="small"
                              variant="ghost"
                              onClick={() => retryDelivery.mutate({ eventId: delivery.event_id, adapter: delivery.adapter })}
                              disabled={retryDelivery.isPending}
                            >
                              <RotateCcw aria-hidden="true" />
                              重试
                            </Button>
                          ) : null}
                        </div>
                      ))}
                    </div>
                  ) : <p className="notification-empty">当前没有启用通道的投递状态。</p>}

                  <div className="notification-attempts">
                    <h4>失败与重试历史</h4>
                    {detail.data.history.length ? (
                      <ul>
                        {detail.data.history.map((attempt, index) => (
                          <li key={`${attempt.adapter}-${attempt.attempted_at}-${index}`}>
                            <span>{channelLabels[attempt.adapter]} · {outcomeLabels[attempt.outcome]}</span>
                            <small>
                              {attempt.error_code ? errorCodeLabel(attempt.error_code, '投递失败') : attempt.http_status ? `HTTP ${attempt.http_status}` : '无错误信息'} · {formatTimestamp(attempt.attempted_at)}
                            </small>
                          </li>
                        ))}
                      </ul>
                    ) : <p className="notification-empty">尚无投递尝试。</p>}
                  </div>
                </>
              ) : null}
            </div>
          </div>
        ) : <p className="notification-empty">尚无通知事件。</p>}
      </section>
    </div>
  )
}
