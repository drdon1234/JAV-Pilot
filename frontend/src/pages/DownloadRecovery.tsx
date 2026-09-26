import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Globe2, Magnet, RefreshCw, WandSparkles } from 'lucide-react'
import { useEffect, useRef, useState } from 'react'

import { useToast } from '../components/ToastProvider'
import { Button, InlineNotice, StatusBadge } from '../components/ui'
import { api } from '../lib/api'
import type {
  DownloadRecoveryMode,
  DownloadRequest,
  DownloadReselection,
  MagnetSelectionPayload,
  WorkMagnet,
} from '../types'

function reselectionKey(candidate: DownloadReselection): string {
  return `${candidate.source_kind}:${candidate.source_id}`
}

function smartSelectionErrorText(value: unknown): string {
  const raw = String(value || '').trim()
  const normalized = raw.toLowerCase()
  if (!raw) return '智能选种未返回失败原因，请刷新后重试'
  if (normalized.includes('download failure changed before replacement selection')) {
    return '原失败任务状态已变化，候选结果没有提交；请刷新下载列表后重试'
  }
  if (normalized.includes('download replacement is no longer open')) {
    return '原失败任务已被其他操作处理，候选结果没有提交；请刷新下载列表'
  }
  if (normalized.includes('no candidate magnet was added')) {
    return 'qBittorrent 没有接受候选磁链，未开始选种'
  }
  if (normalized.includes('could not schedule every candidate')) {
    return '部分候选未被 qBittorrent 调度，可能触及活跃任务上限；未把未调度候选判定为不可用'
  }
  if (normalized.includes('could not compare every candidate')) {
    return '候选磁链未全部完成探测，系统没有把未完成候选判定为不可用'
  }
  if (normalized.includes('no usable smart selection source')) {
    return '候选磁链都没有返回可用元数据或速度，未找到胜者'
  }
  if (normalized.includes('could not be safely removed') || normalized.includes('cleanup')) {
    return `智能选种候选清理不完整：${raw}`
  }
  if (normalized.includes('shutting down')) return '服务正在重启，智能选种尚未完成，请稍后重试'
  return raw
}

function smartSelectionFailureText(payload: MagnetSelectionPayload): string {
  if (payload.replacement_error) {
    return `候选胜者已找到，但新任务提交失败：${smartSelectionErrorText(payload.replacement_error)}`
  }
  return `智能选种失败：${smartSelectionErrorText(payload.error)}`
}

function replacementMagnetRequest(candidate: DownloadReselection, magnet: WorkMagnet): DownloadRequest {
  const source = magnet.source_refs[0]
  if (!source?.uri) throw new Error('磁链来源不可用')
  return {
    magnet: source.uri,
    name: magnet.display_name || source.display_name || candidate.code,
    category: '',
    save_path: '',
    tags: '',
    auto_organize: true,
    replacement_id: candidate.recovery?.replacement_id,
    idempotency_key: candidate.recovery?.idempotency_key,
    result: {
      work_id: `replacement-${candidate.recovery?.replacement_id || candidate.source_id}`,
      canonical_code: candidate.code,
      code: candidate.code,
      title: magnet.display_name || candidate.code,
      release_date: null,
      release_date_conflict: false,
      actors: [],
      tags: [],
      sources: [],
      cover: null,
      magnets: [magnet],
    },
    magnet_info: {
      uri: source.uri,
      info_hash: magnet.info_hash,
      display_name: magnet.display_name || source.display_name,
      trackers: source.trackers,
      exact_length: magnet.size_is_exact ? magnet.size_bytes : null,
      params: {},
      source_id: source.source_id,
    },
  }
}

export function useDownloadReselection() {
  const toast = useToast()
  const queryClient = useQueryClient()
  const discoveryMutation = useMutation({
    mutationFn: ({ candidate, mode }: { candidate: DownloadReselection; mode: DownloadRecoveryMode }) => (
      api.createDownloadReplacement(candidate.source_kind, candidate.source_id, mode)
    ),
    onSuccess: ({ replacement }, { mode }) => {
      void queryClient.invalidateQueries({ queryKey: ['downloads'] })
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      if (!Object.prototype.hasOwnProperty.call(replacement, 'recovery_mode')) {
        toast.push('恢复结果格式过旧，请刷新后重试', 'error')
        return
      }
      if (replacement.status === 'open') {
        const action = mode === 'web'
          ? '寻找 Web 下载源'
          : mode === 'smart_magnet'
            ? '寻找智能选种候选磁链'
            : '探测可用磁链'
        if (replacement.discovery_status === 'queued' || replacement.discovery_status === 'running') {
          toast.push(`${replacement.code} 已开始${action}`, 'info')
        }
        return
      }
      if (replacement.status === 'completed') {
        toast.push(`${replacement.code} 的原失败项已处理，无需再次选择来源`, 'success')
        return
      }
      if (replacement.status === 'expired') {
        toast.push('重新选择入口已失效，请刷新下载列表后重试', 'info')
        return
      }
      if (replacement.status === 'cleanup_failed') {
        toast.push('新任务已创建，旧失败项清理失败；再次选择同一方式只会重试清理', 'info')
        return
      }
      toast.push(`${replacement.code} 的替换任务正在处理，请稍后刷新下载列表`, 'info')
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const webMutation = useMutation({
    mutationFn: (candidate: DownloadReselection) => {
      const recovery = candidate.recovery
      if (!recovery || recovery.web_status !== 'available' || !recovery.web_variant) {
        throw new Error('没有已确认的 Web 下载源')
      }
      return api.addWebDownload(candidate.code, recovery.idempotency_key, {
        variant: recovery.web_variant,
        replacementId: recovery.replacement_id,
      })
    },
    onSuccess: (job) => {
      void queryClient.invalidateQueries({ queryKey: ['downloads'] })
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      if (job.replacement?.status === 'cleanup_failed' || job.replacement_warning) {
        toast.push('Web 任务已加入，但原失败项暂未清理', 'info')
      } else {
        toast.push('Web 下载任务已加入，原失败项已处理', 'success')
      }
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  const [smartSelection, setSmartSelection] = useState<{
    candidate: DownloadReselection
    selectionId: string
  } | null>(null)
  const [smartFailure, setSmartFailure] = useState<{
    key: string
    message: string
  } | null>(null)
  const smartSelectionSubmission = useRef('')
  const smartSelectionStart = useMutation({
    mutationFn: ({ candidate }: { candidate: DownloadReselection }) => {
      const replacementId = candidate.recovery?.replacement_id
      if (!replacementId) throw new Error('智能选种恢复记录不可用')
      return api.startDownloadReplacementSmartSelection(replacementId)
    },
    onSuccess: (payload, variables) => {
      smartSelectionSubmission.current = ''
      setSmartFailure(null)
      if (payload.replacement) {
        void queryClient.invalidateQueries({ queryKey: ['downloads'] })
        void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
        setSmartSelection(null)
        if (payload.replacement.status === 'cleanup_failed') {
          toast.push('智能选种任务已加入，但原失败项暂未清理', 'info')
        } else {
          toast.push('智能选种任务已加入，原失败项已处理', 'success')
        }
        return
      }
      setSmartSelection({ candidate: variables.candidate, selectionId: payload.selection_id })
      toast.push(`${variables.candidate.code} 已开始资源站点智能选种`, 'info')
    },
    onError: (error, variables) => {
      const message = smartSelectionErrorText(error)
      if (variables?.candidate) {
        setSmartFailure({
          key: reselectionKey(variables.candidate),
          message: `智能选种失败：${message}`,
        })
      }
      toast.push(message, 'error')
    },
  })
  const smartSelectionQuery = useQuery({
    queryKey: ['download-reselection-smart-selection', smartSelection?.selectionId || ''],
    queryFn: () => api.magnetSelection(smartSelection?.selectionId || ''),
    enabled: Boolean(smartSelection?.selectionId),
    refetchInterval: (query) => {
      const payload = query.state.data as MagnetSelectionPayload | undefined
      return payload && ['complete', 'failed', 'cancelled'].includes(payload.status) ? false : 1_000
    },
    refetchIntervalInBackground: false,
    retry: false,
  })
  const smartMagnetMutation = useMutation({
    mutationFn: ({ candidate, magnet }: { candidate: DownloadReselection; magnet: WorkMagnet }) => (
      api.addDownload(replacementMagnetRequest(candidate, magnet))
    ),
    onSuccess: (payload) => {
      void queryClient.invalidateQueries({ queryKey: ['downloads'] })
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      setSmartSelection(null)
      setSmartFailure(null)
      if (payload.replacement?.status === 'cleanup_failed' || payload.replacement_warning) {
        toast.push('智能选种任务已加入，但原失败项暂未清理', 'info')
      } else {
        toast.push('智能选种任务已加入，原失败项已处理', 'success')
      }
    },
    onError: (error, variables) => {
      setSmartSelection(null)
      const message = smartSelectionErrorText(error)
      if (variables?.candidate) {
        setSmartFailure({
          key: reselectionKey(variables.candidate),
          message: `候选胜者已找到，但新任务提交失败：${message}`,
        })
      }
      toast.push(message, 'error')
    },
  })
  useEffect(() => {
    const active = smartSelection
    const payload = smartSelectionQuery.data
    if (!active || !payload || smartSelectionSubmission.current === active.selectionId) return
    if (!['complete', 'failed', 'cancelled'].includes(payload.status)) return
    smartSelectionSubmission.current = active.selectionId
    if (payload.replacement_error) {
      const message = smartSelectionFailureText(payload)
      setSmartFailure({ key: reselectionKey(active.candidate), message })
      setSmartSelection(null)
      toast.push(`${active.candidate.code} ${message}`, 'error')
      void queryClient.invalidateQueries({ queryKey: ['downloads'] })
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      return
    }
    if (payload.status !== 'complete' || payload.selection.status !== 'selected') {
      const message = smartSelectionFailureText(payload)
      setSmartFailure({ key: reselectionKey(active.candidate), message })
      setSmartSelection(null)
      toast.push(`${active.candidate.code} ${message}`, 'error')
      void queryClient.invalidateQueries({ queryKey: ['downloads'] })
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      return
    }
    if (payload.replacement) {
      void queryClient.invalidateQueries({ queryKey: ['downloads'] })
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      setSmartSelection(null)
      setSmartFailure(null)
      if (payload.replacement.status === 'cleanup_failed') {
        toast.push('智能选种任务已加入，但原失败项暂未清理', 'info')
      } else {
        toast.push('智能选种任务已加入，原失败项已处理', 'success')
      }
      return
    }
    const selectedHash = payload.selection.selected_info_hash
    const selectedMagnet = active.candidate.recovery?.magnets?.find(
      (magnet) => magnet.info_hash === selectedHash,
    )
    if (!selectedMagnet) {
      setSmartSelection(null)
      const message = '智能选种结果缺少可提交的磁链，请刷新失败任务后重试'
      setSmartFailure({ key: reselectionKey(active.candidate), message })
      toast.push(message, 'error')
      return
    }
    smartMagnetMutation.mutate({ candidate: active.candidate, magnet: selectedMagnet })
  }, [queryClient, smartSelection, smartSelectionQuery.data, smartMagnetMutation, toast])
  const magnetMutation = useMutation({
    mutationFn: ({ candidate, magnet }: { candidate: DownloadReselection; magnet: WorkMagnet }) => {
      return api.addDownload(replacementMagnetRequest(candidate, magnet))
    },
    onSuccess: (payload) => {
      void queryClient.invalidateQueries({ queryKey: ['downloads'] })
      void queryClient.invalidateQueries({ queryKey: ['web-downloads'] })
      if (payload.replacement?.status === 'cleanup_failed' || payload.replacement_warning) {
        toast.push('磁链任务已加入，但原失败项暂未清理', 'info')
      } else {
        toast.push('磁链任务已加入，原失败项已处理', 'success')
      }
    },
    onError: (error) => toast.push((error as Error).message, 'error'),
  })
  return {
    startWeb: (candidate: DownloadReselection) => {
      if (
        candidate.recovery?.status === 'open'
        && candidate.recovery.recovery_mode === 'web'
        && candidate.recovery.discovery_status === 'available'
        && candidate.recovery.web_status === 'available'
        && candidate.recovery.web_variant
      ) {
        webMutation.mutate(candidate)
      } else {
        discoveryMutation.mutate({ candidate, mode: 'web' })
      }
    },
    startSmart: (candidate: DownloadReselection) => {
      setSmartFailure((current) => current?.key === reselectionKey(candidate) ? null : current)
      const recovery = candidate.recovery
      if (
        recovery?.status === 'open'
        && recovery.recovery_mode === 'smart_magnet'
        && recovery.discovery_status === 'available'
        && recovery.magnet_status === 'available'
        && (recovery.magnets?.length ?? 0) > 0
      ) {
        if (
          !recovery.smart_selection_outcome
          || recovery.smart_selection_outcome === 'running'
          || recovery.smart_selection_outcome === 'selected'
        ) {
          smartSelectionStart.mutate({ candidate })
          return
        }
      }
      discoveryMutation.mutate({ candidate, mode: 'smart_magnet' })
    },
    startProbe: (candidate: DownloadReselection) => (
      discoveryMutation.mutate({ candidate, mode: 'manual_magnet' })
    ),
    startMagnet: (candidate: DownloadReselection, magnet: WorkMagnet) => magnetMutation.mutate({ candidate, magnet }),
    pending: (candidate: DownloadReselection | undefined) => Boolean(
      candidate
      && (
        (discoveryMutation.isPending
          && discoveryMutation.variables
          && reselectionKey(candidate) === reselectionKey(discoveryMutation.variables.candidate))
        || (webMutation.isPending
          && webMutation.variables
          && reselectionKey(candidate) === reselectionKey(webMutation.variables))
        || (magnetMutation.isPending
          && magnetMutation.variables
          && reselectionKey(candidate) === reselectionKey(magnetMutation.variables.candidate))
        || (smartSelectionStart.isPending
          && smartSelectionStart.variables
          && reselectionKey(candidate) === reselectionKey(smartSelectionStart.variables.candidate))
        || (smartMagnetMutation.isPending
          && smartMagnetMutation.variables
          && reselectionKey(candidate) === reselectionKey(smartMagnetMutation.variables.candidate))
        || (smartSelection
          && reselectionKey(candidate) === reselectionKey(smartSelection.candidate))
      ),
    ),
    webPending: (candidate: DownloadReselection | undefined) => Boolean(
      candidate
      && (
        (webMutation.isPending
          && webMutation.variables
          && reselectionKey(candidate) === reselectionKey(webMutation.variables))
        || (discoveryMutation.isPending
          && discoveryMutation.variables?.mode === 'web'
          && reselectionKey(candidate) === reselectionKey(discoveryMutation.variables.candidate))
      ),
    ),
    magnetPending: (candidate: DownloadReselection | undefined) => Boolean(
      candidate
      && magnetMutation.isPending
      && magnetMutation.variables
      && reselectionKey(candidate) === reselectionKey(magnetMutation.variables.candidate),
    ),
    smartPending: (candidate: DownloadReselection | undefined) => Boolean(
      candidate
      && ((smartSelectionStart.isPending
        && smartSelectionStart.variables
        && reselectionKey(candidate) === reselectionKey(smartSelectionStart.variables.candidate))
        || (smartMagnetMutation.isPending
          && smartMagnetMutation.variables
          && reselectionKey(candidate) === reselectionKey(smartMagnetMutation.variables.candidate))
        || (smartSelection
          && reselectionKey(candidate) === reselectionKey(smartSelection.candidate))
        || (discoveryMutation.isPending
          && discoveryMutation.variables?.mode === 'smart_magnet'
          && reselectionKey(candidate) === reselectionKey(discoveryMutation.variables.candidate))),
    ),
    smartError: (candidate: DownloadReselection | undefined) => (
      candidate && smartFailure?.key === reselectionKey(candidate) ? smartFailure.message : null
    ),
  }
}

const recoveryProviderLabels: Record<string, string> = {
  missav: 'MissAV',
  jable: 'JableTV',
  supjav: 'SupJav',
}

export function DownloadRecoveryActions({
  candidate,
  busy,
  reselecting,
  webStarting,
  smartStarting,
  smartFailure,
  onSmartSelection,
  onProbeMagnets,
  onWebDownload,
  onMagnetDownload,
  magnetStarting,
}: {
  candidate: DownloadReselection
  busy: boolean
  reselecting: boolean
  webStarting: boolean
  smartStarting: boolean
  smartFailure: string | null
  onSmartSelection: () => void
  onProbeMagnets: () => void
  onWebDownload: () => void
  onMagnetDownload: (magnet: WorkMagnet) => void
  magnetStarting: boolean
}) {
  const recovery = candidate.recovery
  const recoveryMode = recovery?.recovery_mode ?? 'idle'
  const discoveryStatus = recovery?.discovery_status
  const discovering = discoveryStatus === 'queued' || discoveryStatus === 'running'
  const manualStarting = reselecting && !smartStarting && !webStarting && !magnetStarting
  const actionBusy = busy || discovering || reselecting || smartStarting || webStarting
  const magnetAvailable = recovery?.magnet_status === 'available' && (recovery.magnets?.length ?? 0) > 0
  const webAvailable = recovery?.web_status === 'available' && Boolean(recovery.web_variant)
  const continuedDiscovery = useRef('')
  useEffect(() => {
    if (!recovery || recovery.status !== 'open' || discoveryStatus !== 'available') return
    const continuationKey = `${recovery.replacement_id}:${recoveryMode}:${recovery.discovery_finished_at}`
    if (continuedDiscovery.current === continuationKey) return
    if (
      recoveryMode === 'smart_magnet'
      && magnetAvailable
      && (!recovery.smart_selection_outcome || recovery.smart_selection_outcome === 'running')
    ) {
      continuedDiscovery.current = continuationKey
      onSmartSelection()
      return
    }
    if (recoveryMode === 'web' && webAvailable) {
      continuedDiscovery.current = continuationKey
      onWebDownload()
    }
  }, [recovery, recoveryMode, discoveryStatus, magnetAvailable, webAvailable, onSmartSelection, onWebDownload])

  const providerName = (recovery?.web_provider_ids ?? [])
    .map((provider) => recoveryProviderLabels[provider] || provider)
    .join('、')
  const magnetUnavailableText = (() => {
    switch (recovery?.magnet_error_code) {
      case 'configuration':
      case 'configuration_invalid':
        return '资源站点未配置或配置无效'
      case 'challenge_active':
      case 'challenge':
        return '资源站点验证未完成，暂时无法解析'
      case 'upstream_timeout':
      case 'timeout':
        return '资源站点响应超时'
      case 'upstream_rate_limited':
      case 'rate_limited':
        return '资源站点限流，请稍后重试'
      case 'source_unavailable':
        return '资源站点请求或详情解析失败'
      default:
        return '资源站点探测失败，可重试'
    }
  })()
  const smartSelectionRunning = smartStarting
    && recoveryMode === 'smart_magnet'
    && discoveryStatus === 'available'
  const webSubmissionRunning = webStarting
    && recoveryMode === 'web'
    && discoveryStatus === 'available'
  const progressText = smartStarting
    ? smartSelectionRunning ? '正在智能选种并清理其他候选' : '正在查找智能选种候选磁链'
    : webStarting
      ? webSubmissionRunning ? '正在创建 Web 下载任务' : '正在寻找可用 Web 下载源'
      : manualStarting || (discovering && recoveryMode === 'manual_magnet')
        ? '正在探测可用磁链'
        : discovering && recoveryMode === 'smart_magnet'
          ? '正在查找智能选种候选磁链'
          : discovering && recoveryMode === 'web'
            ? '正在寻找可用 Web 下载源'
            : ''
  const resultText = recoveryMode === 'web'
    ? webAvailable
      ? `已找到可用 Web 源：${providerName || '自动选择'}`
      : recovery?.web_status === 'not_found'
        ? 'Web 站点未找到可用资源'
        : recovery?.web_status === 'unavailable'
          ? 'Web 站点暂时不可用'
          : ''
    : recoveryMode === 'smart_magnet' || recoveryMode === 'manual_magnet'
      ? magnetAvailable
      ? `已找到 ${recovery?.magnets?.length ?? recovery?.magnet_count ?? 0} 条新磁链`
      : recovery?.magnet_status === 'not_found'
          ? recovery?.magnet_error_code === 'no_result'
            ? '资源站点未找到匹配作品'
            : '资源站点详情页未提供磁链'
          : recovery?.magnet_status === 'unavailable'
            ? magnetUnavailableText
            : ''
      : ''
  const resultTone = discoveryStatus === 'not_found'
    ? 'danger' as const
    : discoveryStatus === 'inconclusive'
      ? 'warning' as const
      : 'success' as const
  const persistedSmartFailure = recoveryMode === 'smart_magnet'
    ? recovery?.smart_selection_cleanup_status === 'incomplete'
      || recovery?.smart_selection_cleanup_status === 'unknown'
      ? '智能选种未完成候选清理，任务已保留，可稍后重试。'
      : recovery?.smart_selection_outcome === 'not_found'
        ? '智能选种未找到有做种的可用候选，可处理这条失败任务。'
        : recovery?.smart_selection_outcome === 'inconclusive'
          ? '智能选种超时，未能确认全部候选，可处理这条失败任务或重新尝试。'
          : recovery?.smart_selection_outcome === 'cancelled'
            ? '智能选种已取消，可重新尝试。'
            : recovery?.smart_selection_outcome === 'failed'
              ? '智能选种执行失败，可稍后重试。'
              : null
    : null
  const failureText = smartFailure || persistedSmartFailure
  if (recovery && ['submitting', 'replacement_created', 'cleanup_failed'].includes(recovery.status)) {
    const retryCleanup = recovery.recovery_mode === 'web'
      ? onWebDownload
      : recovery.recovery_mode === 'smart_magnet'
        ? onSmartSelection
        : onProbeMagnets
    return (
      <div className="download-recovery-actions">
        <StatusBadge tone={recovery.status === 'cleanup_failed' ? 'warning' : 'info'}>
          {recovery.status === 'cleanup_failed' ? '新任务已创建，旧失败项尚未清理' : '正在确认新任务并清理旧失败项'}
        </StatusBadge>
        <Button className="download-reselection-action" type="button" size="small" variant="secondary" onClick={retryCleanup} disabled={actionBusy}>
          <RefreshCw className={actionBusy ? 'spin' : ''} aria-hidden="true" />
          {actionBusy ? '正在重试清理' : '重试清理旧失败项'}
        </Button>
      </div>
    )
  }
  return (
    <div className="download-recovery-actions">
      {failureText ? (
        <InlineNotice tone={persistedSmartFailure ? 'warning' : 'danger'} role="status" className="download-recovery-failure">
          <span>{failureText}</span>
        </InlineNotice>
      ) : null}
      {progressText ? <StatusBadge tone="info">{progressText}</StatusBadge> : null}
      {!progressText && !persistedSmartFailure && resultText ? <StatusBadge tone={resultTone}>{resultText}</StatusBadge> : null}
      <div className="download-recovery-options" aria-label="重新选择来源方式">
        <Button className="download-reselection-action" type="button" size="small" variant="primary" onClick={onSmartSelection} disabled={actionBusy}>
          <WandSparkles className={smartStarting ? 'spin' : ''} aria-hidden="true" />
          {smartStarting ? '正在智能选种' : '从资源站点智能选种'}
        </Button>
        <Button className="download-reselection-action" type="button" size="small" variant="secondary" onClick={onWebDownload} disabled={actionBusy}>
          <Globe2 className={webStarting ? 'spin' : ''} aria-hidden="true" />
          {webStarting ? '正在准备 Web 下载' : '从 Web 站点智能下载'}
        </Button>
        <Button className="download-reselection-action" type="button" size="small" variant="ghost" onClick={onProbeMagnets} disabled={actionBusy}>
          <RefreshCw className={manualStarting ? 'spin' : ''} aria-hidden="true" />
          {manualStarting ? '正在探测磁链' : '探测可用磁链'}
        </Button>
      </div>
      {recovery?.status === 'open' && recoveryMode === 'manual_magnet' && magnetAvailable ? (
        <div className="download-recovery-magnets" aria-label="探测结果，手动选择下载">
          <span className="download-recovery-manual-label">探测结果，手动选择下载</span>
          {(recovery?.magnets ?? []).map((magnet) => {
            const source = magnet.source_refs[0]
            return (
              <Button key={magnet.info_hash} className="download-reselection-action" type="button" size="small" variant="ghost" onClick={() => onMagnetDownload(magnet)} disabled={actionBusy || magnetStarting}>
                <Magnet aria-hidden="true" />
                {magnetStarting ? '正在加入' : (magnet.display_name || source?.display_name || magnet.info_hash)}
              </Button>
            )
          })}
        </div>
      ) : null}
    </div>
  )
}
