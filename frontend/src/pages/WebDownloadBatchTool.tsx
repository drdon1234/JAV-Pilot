import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { CheckSquare2, ChevronLeft, ChevronRight, Download, RefreshCw, Search, Settings, Trash2, X } from 'lucide-react'
import { useEffect, useState } from 'react'
import { Link, useLocation } from 'react-router-dom'

import { useToast } from '../components/ToastProvider'
import { Button, IconButton, InlineNotice, StatusBadge } from '../components/ui'
import { api, ApiError } from '../lib/api'
import { saveBlob } from '../lib/saveBlob'
import {
  readWebDownloadBatchSession as readStoredWebDownloadBatchSession,
  readWebDownloadBatchRouteState,
  type WebDownloadBatchSession,
  writeWebDownloadBatchSession,
} from '../lib/webDownloadBatchSession'
import {
  webDownloadBatchStatusLabels,
  webDownloadExistingPolicyLabel,
  webDownloadHeightLabel,
  webDownloadVariantLabel,
  webDownloadVariantPriorityLabel,
} from '../lib/webDownloads'
import type {
  WebDownloadBatch,
  WebDownloadBatchChainSummary,
  WebDownloadBatchItem,
  WebDownloadBatchItemIntent,
  WebDownloadBatchQualityStrategy,
  WebDownloadBatchStatus,
  WebDownloadExistingPolicy,
  WebDownloadVariant,
} from '../types'
import { t } from '../lib/i18n'

const activeWebDownloadBatchStatuses = new Set<WebDownloadBatchStatus>(['queued', 'discovering'])

function webDownloadBatchChainSummaryText(chain: WebDownloadBatchChainSummary): string {
  return t('{pages_scanned} 页 · {discovered_count} 部 · 新建 {created_count} · 复用 {reused_count} · 跳过 {skipped_count}', { pages_scanned: chain.pages_scanned, discovered_count: chain.discovered_count, created_count: chain.created_count, reused_count: chain.reused_count, skipped_count: chain.skipped_count })
}

function webDownloadResourceSearchHref(rule: {
  code_or_prefix: string
  start: string | number | null
  end: string | number | null
  max_height: number
  existing_policy: WebDownloadExistingPolicy
  variant_priority: WebDownloadVariant[]
  limit_reached?: boolean
  resume_start?: string | null
  rule_id?: string | null
  rule_revision?: number | null
  revision?: number
}): string {
  const params = new URLSearchParams({
    workspace: 'resource',
    resource_query: rule.code_or_prefix,
    result_limit: '999',
    max_height: String(rule.max_height),
    existing_policy: rule.existing_policy,
    variant_priority: rule.variant_priority.join(','),
  })
  const startValue = rule.limit_reached && rule.resume_start ? rule.resume_start : rule.start
  const start = startValue === null || startValue === undefined ? '' : String(startValue)
  const end = rule.end === null ? '' : String(rule.end)
  if (start) params.set('start', start)
  if (end) params.set('end', end)
  const ruleRevision = rule.rule_revision ?? rule.revision
  if (rule.rule_id && Number.isInteger(ruleRevision) && Number(ruleRevision) > 0) {
    params.set('rule_id', rule.rule_id)
    params.set('rule_revision', String(ruleRevision))
  }
  return `/search?${params.toString()}`
}

function defaultWebDownloadBatchIntent(
  batch: WebDownloadBatch,
  item: WebDownloadBatchItem,
): WebDownloadBatchItemIntent | null {
  const qualityStatus = item.quality_status ?? 'legacy'
  const availableHeights = item.available_heights ?? []
  if (qualityStatus === 'pending' || qualityStatus === 'failed') return null
  if (
    item.quality_strategy === 'selected'
    && item.requested_height !== null
    && (qualityStatus === 'legacy' || availableHeights.includes(item.requested_height))
  ) {
    return {
      code: item.code,
      variant: item.variant,
      quality_strategy: 'selected',
      requested_height: item.requested_height,
    }
  }
  return {
    code: item.code,
    variant: item.variant,
    quality_strategy: 'highest',
    requested_height: item.requested_height ?? batch.max_height,
  }
}

function validatedWebDownloadBatchIntents(
  batch: WebDownloadBatch,
  rawIntents: unknown,
): WebDownloadBatchItemIntent[] | null {
  if (!Array.isArray(rawIntents) || rawIntents.length > batch.items.length) return null
  const items = new Map(batch.items.map((item) => [item.code, item]))
  const seen = new Set<string>()
  const intents: WebDownloadBatchItemIntent[] = []
  for (const raw of rawIntents) {
    if (!raw || typeof raw !== 'object') return null
    const value = raw as Partial<WebDownloadBatchItemIntent>
    const item = typeof value.code === 'string' ? items.get(value.code) : undefined
    if (
      !item
      || seen.has(item.code)
      || value.variant !== item.variant
      || (value.quality_strategy !== 'highest' && value.quality_strategy !== 'selected')
      || !Number.isInteger(value.requested_height)
      || Number(value.requested_height) < 144
      || Number(value.requested_height) > batch.max_height
      || (value.quality_strategy === 'selected'
        && item.quality_status !== 'legacy'
        && !(item.available_heights ?? []).includes(Number(value.requested_height)))
      || item.quality_status === 'pending'
      || item.quality_status === 'failed'
    ) return null
    seen.add(item.code)
    intents.push({
      code: item.code,
      variant: item.variant,
      quality_strategy: value.quality_strategy,
      requested_height: Number(value.requested_height),
    })
  }
  return batch.items.flatMap((item) => intents.filter((intent) => intent.code === item.code))
}

function validateWebDownloadBatchSession(
  value: WebDownloadBatchSession | null,
): WebDownloadBatchSession | null {
  if (
    !value
    || !(value.batch.status in webDownloadBatchStatusLabels)
    || (value.itemIntents !== undefined && validatedWebDownloadBatchIntents(value.batch, value.itemIntents) === null)
  ) return null
  const recoveredIntents = value.itemIntents !== undefined && value.intentsInitialized !== false
    ? validatedWebDownloadBatchIntents(value.batch, value.itemIntents) ?? []
    : undefined
  const session = {
    batch: value.batch,
    previewToken: value.previewToken,
    ...(recoveredIntents ? { itemIntents: recoveredIntents } : {}),
  }
  return session
}

function skipStoredWebDownloadBatchPreview(routeState: unknown): boolean {
  return Boolean(
    routeState
    && typeof routeState === 'object'
    && (routeState as Record<string, unknown>).skipStoredWebDownloadBatchPreview === true,
  )
}

function webDownloadBatchExpiryDelay(batch: WebDownloadBatch | null): number | false {
  if (!batch || batch.status !== 'ready' || batch.expires_at === null) return false
  const expiresAt = Number(batch.expires_at)
  if (!Number.isFinite(expiresAt)) return 1_000
  return Math.max(1_000, Math.min(60_000, expiresAt * 1_000 - Date.now() + 250))
}

export function WebDownloadBatchTool({
  serviceReady,
}: {
  serviceReady: boolean
}) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const location = useLocation()
  const [batchSession, setBatchSessionState] = useState<WebDownloadBatchSession | null>(() => {
    const storedSession = readStoredWebDownloadBatchSession()
    const routeSession = validateWebDownloadBatchSession(readWebDownloadBatchRouteState(location.state))
    return skipStoredWebDownloadBatchPreview(location.state)
      ? routeSession
      : validateWebDownloadBatchSession(storedSession) ?? routeSession
  })
  const [batchIntents, setBatchIntents] = useState<{
    batchId: string
    intents: WebDownloadBatchItemIntent[]
    qualityComplete: boolean
  }>(() => ({
    batchId: batchSession?.batch.status === 'ready' ? batchSession.batch.batch_id : '',
    intents: batchSession?.batch.status === 'ready'
      ? batchSession.itemIntents
        ?? batchSession.batch.items.flatMap((item) => item.selected !== false
          ? [defaultWebDownloadBatchIntent(batchSession.batch, item)].filter(
            (intent): intent is WebDownloadBatchItemIntent => intent !== null,
          )
          : [])
      : [],
    qualityComplete: Boolean(batchSession?.batch.quality_complete),
  }))
  const [rulePanelOpen, setRulePanelOpen] = useState(false)
  const [historyPanelOpen, setHistoryPanelOpen] = useState(false)
  const [chainOffset, setChainOffset] = useState(0)
  const [selectedRootChainId, setSelectedRootChainId] = useState('')
  const [chainPageOffset, setChainPageOffset] = useState(0)
  const batch = batchSession?.batch ?? null
  const previewToken = batchSession?.previewToken ?? ''

  function setBatchSession(session: WebDownloadBatchSession | null) {
    setBatchSessionState(session)
    writeWebDownloadBatchSession(session)
  }

  const batchQuery = useQuery({
    queryKey: ['web-download-batch', batch?.batch_id || 'idle'],
    queryFn: () => api.webDownloadBatch(batch?.batch_id || ''),
    enabled: Boolean(batch),
    refetchInterval: (query) => {
      const current = query.state.data?.batch ?? batch
      if (current && activeWebDownloadBatchStatuses.has(current.status)) return 1_500
      if (current?.status === 'ready' && !current.quality_complete) return 1_500
      return webDownloadBatchExpiryDelay(current)
    },
    refetchIntervalInBackground: false,
    retry: false,
  })
  const polledBatch = batchQuery.data?.batch
  const currentBatch = polledBatch?.batch_id === batch?.batch_id
    ? polledBatch
    : batch
  const backgroundResolutionBatch = Boolean(
    currentBatch?.items.some((item) => item.quality_status === 'legacy'),
  )
  const batchItemSignature = currentBatch?.items
    .map((item) => [
      item.code,
      item.variant,
      item.selected ? '1' : '0',
      item.quality_status,
      item.quality_strategy,
      item.requested_height ?? '',
      (item.available_heights ?? []).join(','),
    ].join(':'))
    .join('|') ?? ''

  useEffect(() => {
    if (!currentBatch || currentBatch.status !== 'ready') return
    setBatchIntents((current) => {
      if (current.batchId !== currentBatch.batch_id) {
        return {
          batchId: currentBatch.batch_id,
          intents: currentBatch.items.flatMap((item) => item.selected !== false
            ? [defaultWebDownloadBatchIntent(currentBatch, item)].filter(
              (intent): intent is WebDownloadBatchItemIntent => intent !== null,
            )
            : []),
          qualityComplete: currentBatch.quality_complete,
        }
      }
      const recovered = validatedWebDownloadBatchIntents(currentBatch, current.intents) ?? []
      if (!currentBatch.quality_complete) {
        return {
          ...current,
          intents: recovered,
          qualityComplete: false,
        }
      }
      if (current.qualityComplete) {
        return recovered.length === current.intents.length ? current : { ...current, intents: recovered }
      }
      const recoveredCodes = new Set(recovered.map((intent) => intent.code))
      const newlyReady = currentBatch.items.flatMap((item) => (
        item.selected !== false && !recoveredCodes.has(item.code)
          ? [defaultWebDownloadBatchIntent(currentBatch, item)].filter(
            (intent): intent is WebDownloadBatchItemIntent => intent !== null,
          )
          : []
      ))
      const intents = [...recovered, ...newlyReady]
      return { batchId: currentBatch.batch_id, intents, qualityComplete: true }
    })
  }, [batchItemSignature, currentBatch?.batch_id, currentBatch?.existing_policy, currentBatch?.status])

  useEffect(() => {
    if (!(batchQuery.error instanceof ApiError) || batchQuery.error.status !== 404 || !batch) return
    setBatchSessionState(null)
    writeWebDownloadBatchSession(null)
    queryClient.removeQueries({ queryKey: ['web-download-batch', batch.batch_id], exact: true })
    toast.push(t('批量预览已不存在，请前往资源搜索重新创建。'), 'info')
  }, [batch, batchQuery.error, queryClient, toast])

  const batchAction = useMutation({
    mutationFn: ({
      batchId,
      name,
      token,
      itemIntents,
    }: {
      batchId: string
      name: 'cancel' | 'commit' | 'remove'
      token?: string
      itemIntents?: WebDownloadBatchItemIntent[]
    }) => name === 'commit'
      ? api.webDownloadBatchAction(batchId, name, token, itemIntents)
      : api.webDownloadBatchAction(batchId, name, token),
    onSuccess: (payload, variables) => {
      queryClient.setQueryData(['web-download-batch', variables.batchId], payload)
      if (variables.name === 'commit') {
        const excludedSummary = payload.batch.excluded_count ? t('，未选 {excluded_count}', { excluded_count: payload.batch.excluded_count }) : ''
        toast.push(
          t('第 {value} 批已加入队列：新建 {created_count}，复用 {reused_count}，已完成跳过 {skipped_count}{excludedSummary}', { value: payload.batch.page ?? 1, created_count: payload.batch.created_count, reused_count: payload.batch.reused_count, skipped_count: payload.batch.skipped_count, excludedSummary }),
          'success',
        )
        if (payload.batch.has_more) {
          setBatchSession({ batch: payload.batch, previewToken })
          void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
          return
        }
        setBatchSession(null)
        queryClient.removeQueries({ queryKey: ['web-download-batch', variables.batchId], exact: true })
        void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
        return
      }
      if (variables.name === 'remove') {
        toast.push(t('批量预览已清除'), 'success')
        setBatchSession(null)
        queryClient.removeQueries({ queryKey: ['web-download-batch', variables.batchId], exact: true })
        return
      }
      setBatchSession({ batch: payload.batch, previewToken })
      toast.push(t('已取消批量任务'), 'success')
    },
    onError: (error, variables) => {
      toast.push((error as Error).message, 'error')
      if (error instanceof ApiError && error.status === 404) {
        setBatchSession(null)
      } else if (variables.name === 'commit') {
        if (currentBatch) {
          setBatchSession({
            batch: currentBatch,
            previewToken,
            ...(variables.name === 'commit' && variables.itemIntents
              ? { itemIntents: variables.itemIntents }
              : {}),
          })
        }
        void batchQuery.refetch()
      }
    },
  })
  const rulesQuery = useQuery({
    queryKey: ['web-download-batch-rules'],
    queryFn: api.webDownloadBatchRules,
    enabled: rulePanelOpen,
    retry: false,
  })
  const chainsQuery = useQuery({
    queryKey: ['web-download-batch-chains', chainOffset],
    queryFn: () => api.webDownloadBatchChains({ limit: 10, offset: chainOffset }),
    enabled: historyPanelOpen,
    retry: false,
  })
  const chainQuery = useQuery({
    queryKey: ['web-download-batch-chain', selectedRootChainId, chainPageOffset],
    queryFn: () => api.webDownloadBatchChain(selectedRootChainId, {
      pageLimit: 8,
      pageOffset: chainPageOffset,
    }),
    enabled: historyPanelOpen && Boolean(selectedRootChainId),
    retry: false,
  })
  useEffect(() => {
    if (!chainsQuery.data) return
    const lastOffset = Math.max(0, Math.floor(Math.max(0, chainsQuery.data.count - 1) / 10) * 10)
    if (chainOffset > lastOffset) setChainOffset(lastOffset)
  }, [chainOffset, chainsQuery.data])
  useEffect(() => {
    if (!chainQuery.data) return
    const lastOffset = Math.max(0, Math.floor(Math.max(0, chainQuery.data.page_count - 1) / 8) * 8)
    if (chainPageOffset > lastOffset) setChainPageOffset(lastOffset)
  }, [chainPageOffset, chainQuery.data])
  const cancelChain = useMutation({
    mutationFn: api.cancelWebDownloadBatchChain,
    onSuccess: (payload) => {
      toast.push(t('已取消 {value} 的未提交预览', { value: payload.root_chain_id.slice(0, 8) }), 'success')
      void queryClient.invalidateQueries({ queryKey: ['web-download-batch-chains'] })
      void queryClient.invalidateQueries({ queryKey: ['web-download-batch-chain', payload.root_chain_id] })
      if (currentBatch?.root_chain_id === payload.root_chain_id) void batchQuery.refetch()
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const exportChain = useMutation({
    mutationFn: api.exportWebDownloadBatchChain,
    onSuccess: (payload) => {
      const blob = new Blob([`${JSON.stringify(payload, null, 2)}\n`], { type: 'application/json' })
      saveBlob(blob, `jav-pilot-batch-${payload.prefix}-${payload.root_chain_id.slice(0, 8)}.json`)
      toast.push(t('批次范围已导出'), 'success')
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })

  function updateBatchIntents(intents: WebDownloadBatchItemIntent[]) {
    if (!currentBatch || currentBatch.status !== 'ready') return
    const ordered = currentBatch.items.flatMap((item) => intents.filter((intent) => intent.code === item.code))
    setBatchIntents({
      batchId: currentBatch.batch_id,
      intents: ordered,
      qualityComplete: currentBatch.quality_complete,
    })
    setBatchSession({
      batch: currentBatch,
      previewToken,
      itemIntents: ordered,
    })
  }

  function toggleBatchCode(code: string, checked: boolean) {
    if (!currentBatch || currentBatch.status !== 'ready') return
    const item = currentBatch.items.find((candidate) => candidate.code === code)
    if (!item) return
    const next = batchIntents.intents.filter((intent) => intent.code !== code)
    if (checked) {
      const intent = defaultWebDownloadBatchIntent(currentBatch, item)
      if (intent) next.push(intent)
    }
    updateBatchIntents(next)
  }

  function updateBatchItemQuality(code: string, value: string) {
    if (!currentBatch || currentBatch.status !== 'ready') return
    const item = currentBatch.items.find((candidate) => candidate.code === code)
    if (!item) return
    const strategy: WebDownloadBatchQualityStrategy = value === 'highest' ? 'highest' : 'selected'
    const requestedHeight = strategy === 'highest' ? currentBatch.max_height : Number(value)
    if (
      !Number.isInteger(requestedHeight)
      || (strategy === 'selected' && !(item.available_heights ?? []).includes(requestedHeight))
    ) return
    updateBatchIntents([
      ...batchIntents.intents.filter((intent) => intent.code !== code),
      { code, variant: item.variant, quality_strategy: strategy, requested_height: requestedHeight },
    ])
  }

  function runBatchAction(name: 'cancel' | 'commit' | 'remove') {
    if (!currentBatch) return
    const itemIntents = name === 'commit'
      ? currentBatch.items.flatMap((item) => batchIntents.intents.filter((intent) => intent.code === item.code))
      : undefined
    if (name === 'commit' && !itemIntents?.length) return
    if (name === 'commit' && currentBatch.has_more) {
      setBatchSession({
        batch: currentBatch,
        previewToken,
        ...(name === 'commit' && itemIntents ? { itemIntents } : {}),
      })
    }
    batchAction.reset()
    batchAction.mutate({
      batchId: currentBatch.batch_id,
      name,
      ...(name === 'commit' ? { token: previewToken } : {}),
      ...(itemIntents ? { itemIntents } : {}),
    })
  }

  useEffect(() => {
    if (!currentBatch || batchAction.isPending) return
    if (currentBatch.status === 'committed' && !currentBatch.has_more) {
      setBatchSession(null)
      queryClient.removeQueries({ queryKey: ['web-download-batch', currentBatch.batch_id], exact: true })
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      return
    }
  }, [batchAction.isPending, currentBatch, queryClient])

  const actionError = batchAction.error
  const clearableStatuses = new Set<WebDownloadBatchStatus>([
    'ready',
    'too_many',
    'incomplete',
    'failed',
    'cancelled',
    'expired',
    'committed',
  ])
  const canClear = Boolean(
    currentBatch?.can_remove
    && currentBatch.status !== 'committed'
    && clearableStatuses.has(currentBatch.status),
  )
  const codes = currentBatch?.items.map((item) => item.code) ?? []
  const selectableItems = currentBatch?.items.filter(
    (item) => !item.quality_status || item.quality_status === 'ready' || item.quality_status === 'legacy',
  ) ?? []
  const currentIntents = batchIntents.batchId === currentBatch?.batch_id ? batchIntents.intents : []
  const intentByCode = new Map(currentIntents.map((intent) => [intent.code, intent]))
  const selectedCodeSet = new Set(currentIntents.map((intent) => intent.code))
  const selectedCodes = codes.filter((code) => selectedCodeSet.has(code))
  const allSelected = selectableItems.length > 0 && selectedCodes.length === selectableItems.length
  const statusTone = currentBatch?.status === 'ready' && currentBatch.count > 0
    ? 'success' as const
    : currentBatch?.status === 'ready'
      ? 'warning' as const
    : currentBatch?.status === 'failed' || currentBatch?.status === 'too_many'
      ? 'danger' as const
      : currentBatch?.status === 'incomplete' || currentBatch?.status === 'cancelled' || currentBatch?.status === 'expired'
        ? 'warning' as const
        : 'info' as const

  return (
    <section className="web-batch-tool" aria-labelledby="web-batch-title">
      <div className="web-batch-heading">
        <div>
          <h2 id="web-batch-title">{t('批量下载管理')}</h2>
          <p>{t('搜索页只确认可用来源；这里确认后台解析规则、提交任务并查看历史。')}</p>
        </div>
        {currentBatch ? (
          <StatusBadge tone={statusTone}>{webDownloadBatchStatusLabels[currentBatch.status]}</StatusBadge>
        ) : null}
      </div>

      {actionError ? (
        <InlineNotice tone="danger" role="alert">
          {(actionError as Error).message || t('批量操作失败，请重试。')}
        </InlineNotice>
      ) : null}

      {currentBatch ? (
        <div
          className={`web-batch-state web-batch-${currentBatch.status}`}
        >
          <div className="web-batch-state-main">
            <div
              role={currentBatch.status === 'failed' ? 'alert' : 'status'}
              aria-live={currentBatch.status === 'failed' ? undefined : 'polite'}
              aria-atomic="true"
            >
              <strong>
                {currentBatch.status === 'ready'
                  ? currentBatch.count > 0
                    ? currentBatch.quality_complete
                      ? backgroundResolutionBatch
                        ? t('已确认 {count} 部作品存在对应源', { count: currentBatch.count })
                        : t('第 {page} 批找到 {count} 部作品', { page: currentBatch.page, count: currentBatch.count })
                      : t('第 {page} 批已找到作品，正在解析画质', { page: currentBatch.page })
                    : t('未找到可用作品')
                  : webDownloadBatchStatusLabels[currentBatch.status]}
              </strong>
              <span>
                {currentBatch.status === 'queued'
                  ? t('已有资源发现任务正在排队，尚未创建下载任务。')
                  : currentBatch.status === 'discovering'
                    ? t('已有资源发现任务仍在后台运行，尚未创建下载任务。')
                    : currentBatch.status === 'ready'
                      ? currentBatch.count > 0
                        ? currentBatch.quality_complete
                          ? backgroundResolutionBatch
                            ? t('确认后立即加入后台任务，按“{value}”和画质上限 {value2} 自动解析并下载；已有作品按“{value3}”处理。', { value: webDownloadVariantPriorityLabel(currentBatch.variant_priority), value2: webDownloadHeightLabel(currentBatch.max_height), value3: webDownloadExistingPolicyLabel(currentBatch.existing_policy) })
                            : t('{value}可逐项选择最高可用或明确画质；提交后按“{value2}”处理。', { value: currentBatch.has_more
                              ? t('本页提交后，后续资源可回到搜索页继续处理。')
                              : '', value2: webDownloadExistingPolicyLabel(currentBatch.existing_policy) })
                          : t('正在逐项确认可用画质，完成前不会创建下载任务。')
                        : t('当前预览没有可提交的作品，可清除后回到资源搜索。')
                      : currentBatch.status === 'too_many'
                        ? t('发现 {count} 部作品，超过原批次上限。请回到资源搜索调整条件。', { count: currentBatch.count })
                        : currentBatch.status === 'incomplete'
                          ? t('原资源发现结果不完整，可清除后回到资源搜索继续。')
                          : currentBatch.status === 'failed'
                            ? t('原资源发现任务失败，可清除后回到资源搜索。')
                            : currentBatch.status === 'cancelled'
                              ? t('资源发现已取消，可清除该预览。')
                              : currentBatch.status === 'expired'
                                ? t('预览已失效，请清除后回到资源搜索。')
                                : currentBatch.status === 'committed' && currentBatch.limit_reached
                                  ? t('原批次已达到 {page_budget} 页预算，可在资源搜索中从 {value} 继续。', { page_budget: currentBatch.page_budget, value: currentBatch.resume_start ?? t('导出位置') })
                                  : currentBatch.status === 'committed' && currentBatch.has_more
                                    ? t('本页已入队，后续资源请在搜索页继续处理。')
                                  : t('批量任务已处理。')}
              </span>
              {currentBatch.error ? <span className="task-issue">{currentBatch.error}</span> : null}
            </div>
            <div className="web-batch-state-actions">
              {currentBatch.can_cancel ? (
                <Button type="button" size="small" variant="ghost" onClick={() => runBatchAction('cancel')} disabled={batchAction.isPending}>
                  <X aria-hidden="true" />
                  {batchAction.isPending && batchAction.variables?.name === 'cancel' ? t('正在取消') : t('取消批量任务')}
                </Button>
              ) : null}
              {currentBatch.status === 'ready' && currentBatch.can_commit ? (
                <Button
                  type="button"
                  size="small"
                  variant="primary"
                  title={backgroundResolutionBatch
                    ? t('确认后立即创建后台任务，画质和清单由任务按已保存规则解析')
                    : t('将本页按{value}、最高可用且上限 {value2} 加入下载队列', { value: webDownloadExistingPolicyLabel(currentBatch.existing_policy), value2: webDownloadHeightLabel(currentBatch.max_height) })}
                  onClick={() => runBatchAction('commit')}
                  disabled={batchAction.isPending || !previewToken || selectedCodes.length < 1 || !serviceReady || !currentBatch.quality_complete}
                >
                  <Download aria-hidden="true" />
                  {batchAction.isPending && batchAction.variables?.name === 'commit'
                    ? t('正在加入')
                    : allSelected
                      ? backgroundResolutionBatch ? t('确认并加入后台任务') : t('加入本页下载队列')
                      : t('加入本页已选 {count} 项', { count: selectedCodes.length })}
                </Button>
              ) : null}
              {currentBatch.status !== 'ready' ? (
                <Link className="button button-ghost button-small" to={webDownloadResourceSearchHref(currentBatch)}>
                  <Search aria-hidden="true" />
                  {t('前往资源搜索')}
                </Link>
              ) : null}
              {canClear ? (
                <IconButton
                  label={t('清除批量预览')}
                  title={t('清除批量预览')}
                  size="small"
                  className="danger-icon"
                  onClick={() => runBatchAction('remove')}
                  disabled={batchAction.isPending}
                >
                  <Trash2 aria-hidden="true" />
                </IconButton>
              ) : null}
              {currentBatch.status === 'committed' ? (
                <IconButton
                  label={t('关闭批量状态')}
                  title={t('关闭批量状态')}
                  size="small"
                  onClick={() => setBatchSession(null)}
                  disabled={batchAction.isPending}
                >
                  <X aria-hidden="true" />
                </IconButton>
              ) : null}
            </div>
          </div>

          {currentBatch.status === 'ready' && codes.length ? (
            <div className="web-batch-results">
              <div className="web-batch-results-toolbar">
                <div>
                  <StatusBadge tone="info">
                    {backgroundResolutionBatch ? t('后台解析') : t('单项画质')} {t('· 上限 {value} · {value2}', { value: webDownloadHeightLabel(currentBatch.max_height), value2: webDownloadExistingPolicyLabel(currentBatch.existing_policy) })}
                  </StatusBadge>
                  <StatusBadge>{webDownloadVariantPriorityLabel(currentBatch.variant_priority)}</StatusBadge>
                  <span className="web-batch-selection-count" role="status" aria-live="polite" aria-atomic="true">
                    {t('本页已选 {count} / {count2}', { count: selectedCodes.length, count2: codes.length })}
                  </span>
                </div>
                <div className="web-batch-selection-actions">
                  <Button
                    type="button"
                    size="small"
                    variant="ghost"
                    disabled={allSelected || batchAction.isPending}
                    onClick={() => updateBatchIntents(selectableItems.flatMap((item) => {
                      const intent = defaultWebDownloadBatchIntent(currentBatch, item)
                      return intent ? [intent] : []
                    }))}
                  >
                    <CheckSquare2 aria-hidden="true" />
                    {t('全选')}
                  </Button>
                  <Button
                    type="button"
                    size="small"
                    variant="ghost"
                    disabled={!selectedCodes.length || batchAction.isPending}
                    onClick={() => updateBatchIntents([])}
                  >
                    <X aria-hidden="true" />
                    {t('清空')}
                  </Button>
                </div>
              </div>
              <ul className="web-batch-result-list" aria-label={t('批量预览番号')}>
                {currentBatch.items.map((item) => (
                  <li className={selectedCodeSet.has(item.code) ? 'selected' : ''} key={item.code}>
                    <label className="web-batch-result-option">
                      <input
                        type="checkbox"
                        aria-label={item.code}
                        checked={selectedCodeSet.has(item.code)}
                        disabled={batchAction.isPending || item.quality_status === 'pending' || item.quality_status === 'failed'}
                        onChange={(event) => toggleBatchCode(item.code, event.target.checked)}
                      />
                      <code>{item.code}</code>
                      <StatusBadge>{webDownloadVariantLabel(item.variant)}</StatusBadge>
                      {item.quality_status === 'pending' ? (
                        <span>{t('解析中')}</span>
                      ) : item.quality_status === 'failed' ? (
                        <span>{t('画质不可用')}</span>
                      ) : item.quality_status === 'legacy' ? (
                        <span>
                          {intentByCode.get(item.code)?.quality_strategy === 'selected'
                            ? t('{value} · 后台验证', { value: webDownloadHeightLabel(intentByCode.get(item.code)?.requested_height ?? currentBatch.max_height) })
                            : t('最高可用 · 上限 {value}', { value: webDownloadHeightLabel(currentBatch.max_height) })}
                        </span>
                      ) : (
                        <select
                          aria-label={t('{code} 画质', { code: item.code })}
                          value={intentByCode.get(item.code)?.quality_strategy === 'selected'
                            ? String(intentByCode.get(item.code)?.requested_height)
                            : 'highest'}
                          disabled={batchAction.isPending || !selectedCodeSet.has(item.code) || !item.quality_status}
                          onChange={(event) => updateBatchItemQuality(item.code, event.target.value)}
                        >
                          <option value="highest">{t('最高可用')}</option>
                          {(item.available_heights ?? []).map((height) => (
                            <option value={height} key={height}>{webDownloadHeightLabel(height)}</option>
                          ))}
                        </select>
                      )}
                    </label>
                  </li>
                ))}
              </ul>
            </div>
          ) : null}

          {batchQuery.isError && (
            activeWebDownloadBatchStatuses.has(currentBatch.status)
            || (currentBatch.status === 'ready' && !currentBatch.quality_complete)
          ) ? (
            <InlineNotice tone="warning" role="alert">
              <div>
                <span>{t('暂时无法刷新批量状态。')}</span>
                <Button type="button" size="small" variant="ghost" onClick={() => void batchQuery.refetch()} disabled={batchQuery.isFetching}>
                  <RefreshCw className={batchQuery.isFetching ? 'spin' : ''} aria-hidden="true" />
                  {t('重试')}
                </Button>
              </div>
            </InlineNotice>
          ) : null}
        </div>
      ) : null}

      <div className="web-batch-state">
        <div className="web-batch-state-main">
          <div>
            <strong>{t('批量规则')}</strong>
            <span>{t('已保存规则可在资源搜索中继续使用。')}</span>
          </div>
          <Button type="button" size="small" variant="ghost" onClick={() => setRulePanelOpen((open) => !open)}>
            <Settings aria-hidden="true" />
            {rulePanelOpen ? t('收起规则') : t('查看规则')}
          </Button>
        </div>
        {rulePanelOpen ? (
          <div className="web-batch-results">
            {rulesQuery.isError ? (
              <InlineNotice tone="warning" role="alert">{t('批量规则暂不可用，可稍后重试。')}</InlineNotice>
            ) : null}
            {rulesQuery.data?.rules.length ? (
              <ul className="web-batch-result-list" aria-label={t('已保存的批量规则')} style={{ gridTemplateColumns: 'minmax(0, 1fr)' }}>
                {rulesQuery.data.rules.map((rule) => (
                  <li key={rule.rule_id}>
                    <div className="web-batch-result-option">
                      <code>{rule.name}</code>
                      <span>{rule.code_or_prefix} · {rule.start && rule.end ? `${rule.start}–${rule.end}` : t('全部范围')} · {webDownloadVariantPriorityLabel(rule.variant_priority)} · {rule.default_quality_strategy === 'highest' ? t('最高可用') : webDownloadHeightLabel(rule.default_height ?? rule.max_height)}</span>
                      <Link className="button button-ghost button-small" to={webDownloadResourceSearchHref(rule)}>
                        <Search aria-hidden="true" />
                        {t('前往资源搜索')}
                      </Link>
                    </div>
                  </li>
                ))}
              </ul>
            ) : rulesQuery.isSuccess ? (
              <span className="web-batch-range-hint">{t('尚未保存批量规则。')}</span>
            ) : null}
          </div>
        ) : null}
      </div>

      <div className="web-batch-state">
        <div className="web-batch-state-main">
          <div>
            <strong>{t('批次历史')}</strong>
            <span>{t('按 root chain 查看分页结果、失败游标和续跑位置。')}</span>
          </div>
          <Button type="button" size="small" variant="ghost" onClick={() => setHistoryPanelOpen((open) => !open)}>
            <RefreshCw aria-hidden="true" />
            {historyPanelOpen ? t('收起历史') : t('查看历史')}
          </Button>
        </div>
        {historyPanelOpen ? (
          <div className="web-batch-results">
            {chainsQuery.isError ? (
              <InlineNotice tone="warning" role="alert">
                <div>
                  <span>{t('批次历史暂不可用。')}</span>
                  <Button type="button" size="small" variant="ghost" onClick={() => void chainsQuery.refetch()}>{t('重试')}</Button>
                </div>
              </InlineNotice>
            ) : null}
            {chainsQuery.data?.chains.length ? (
              <ul className="web-batch-result-list" aria-label={t('批次链历史')} style={{ gridTemplateColumns: 'minmax(0, 1fr)' }}>
                {chainsQuery.data.chains.map((chain) => (
                  <li className={selectedRootChainId === chain.root_chain_id ? 'selected' : ''} key={chain.root_chain_id}>
                    <div className="web-batch-result-option">
                      <code>{chain.code_or_prefix}</code>
                      <StatusBadge tone={chain.failed_count ? 'danger' : chain.status === 'committed' ? 'success' : 'info'}>{webDownloadBatchStatusLabels[chain.status]}</StatusBadge>
                      <span>{webDownloadBatchChainSummaryText(chain)}</span>
                      {chain.limit_reached ? <span>{t('续跑 {resume_start}', { resume_start: chain.resume_start })}</span> : null}
                      <Button type="button" size="small" variant="ghost" onClick={() => {
                        setSelectedRootChainId(chain.root_chain_id)
                        setChainPageOffset(0)
                      }}>{t('查看分页')}</Button>
                      <IconButton label={t('导出批次 {code_or_prefix}', { code_or_prefix: chain.code_or_prefix })} size="small" disabled={exportChain.isPending} onClick={() => exportChain.mutate(chain.root_chain_id)}>
                        <Download aria-hidden="true" />
                      </IconButton>
                      <IconButton label={t('取消批次 {code_or_prefix}', { code_or_prefix: chain.code_or_prefix })} size="small" className="danger-icon" disabled={cancelChain.isPending} onClick={() => cancelChain.mutate(chain.root_chain_id)}>
                        <X aria-hidden="true" />
                      </IconButton>
                    </div>
                  </li>
                ))}
              </ul>
            ) : chainsQuery.isSuccess ? (
              <span className="web-batch-range-hint">{t('暂无批次历史。')}</span>
            ) : null}
            {chainsQuery.data && chainsQuery.data.count > chainsQuery.data.limit ? (
              <div className="history-pager" role="group" aria-label={t('批次链分页')}>
                <Button type="button" size="small" variant="ghost" disabled={chainOffset === 0 || chainsQuery.isFetching} onClick={() => setChainOffset(Math.max(0, chainOffset - 10))}>
                  <ChevronLeft aria-hidden="true" />{t('上一页')}
                </Button>
                <span>{chainOffset + 1}–{Math.min(chainOffset + 10, chainsQuery.data.count)} / {chainsQuery.data.count}</span>
                <Button type="button" size="small" variant="ghost" disabled={!chainsQuery.data.has_more || chainsQuery.isFetching} onClick={() => setChainOffset(chainOffset + 10)}>
                  {t('下一页')}<ChevronRight aria-hidden="true" />
                </Button>
              </div>
            ) : null}
            {chainQuery.data ? (
              <div className="web-batch-results">
                <div className="web-batch-results-toolbar">
                  <div>
                    <StatusBadge tone={chainQuery.data.failed_count ? 'danger' : 'info'}>{t('共 {page_count} 页', { page_count: chainQuery.data.page_count })}</StatusBadge>
                    {chainQuery.data.failed_cursors.length ? <span className="web-batch-selection-count">{t('失败页')} {chainQuery.data.failed_pages.join(t('、'))}</span> : null}
                  </div>
                </div>
                <ul className="web-batch-result-list" aria-label={t('批次链分页结果')} style={{ gridTemplateColumns: 'minmax(0, 1fr)' }}>
                  {chainQuery.data.pages.map((page) => (
                    <li key={page.batch_id}>
                      <div className="web-batch-result-option">
                        <code>{t('第 {page} 页', { page: page.page })}</code>
                        <StatusBadge tone={page.status === 'failed' ? 'danger' : page.status === 'committed' ? 'success' : 'info'}>{webDownloadBatchStatusLabels[page.status]}</StatusBadge>
                        <span>{t('{count} 部 · 新建 {created_count} · 复用 {reused_count} · 跳过 {skipped_count}', { count: page.count, created_count: page.created_count, reused_count: page.reused_count, skipped_count: page.skipped_count })}</span>
                      </div>
                    </li>
                  ))}
                </ul>
                {chainQuery.data.page_count > chainQuery.data.page_limit ? (
                  <div className="history-pager" role="group" aria-label={t('批次页分页')}>
                    <Button type="button" size="small" variant="ghost" disabled={chainPageOffset === 0 || chainQuery.isFetching} onClick={() => setChainPageOffset(Math.max(0, chainPageOffset - 8))}>
                      <ChevronLeft aria-hidden="true" />{t('上一页')}
                    </Button>
                    <span>{chainPageOffset + 1}–{Math.min(chainPageOffset + 8, chainQuery.data.page_count)} / {chainQuery.data.page_count}</span>
                    <Button type="button" size="small" variant="ghost" disabled={!chainQuery.data.has_more_pages || chainQuery.isFetching} onClick={() => setChainPageOffset(chainPageOffset + 8)}>
                      {t('下一页')}<ChevronRight aria-hidden="true" />
                    </Button>
                  </div>
                ) : null}
              </div>
            ) : null}
          </div>
        ) : null}
      </div>
    </section>
  )
}
