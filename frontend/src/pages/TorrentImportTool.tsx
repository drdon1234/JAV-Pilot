import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { Download, ScanSearch, X } from 'lucide-react'
import { type FormEvent, useRef, useState } from 'react'

import { useToast } from '../components/ToastProvider'
import { Button, InlineNotice, StatusBadge, Toggle } from '../components/ui'
import { api, ApiError } from '../lib/api'
import { formatBytes } from '../lib/format'
import type {
  DownloadImportError,
  DownloadImportMetadataStatus,
  DownloadImportPayload,
  DownloadImportPreviewItem,
  DownloadImportPreviewPayload,
  DownloadImportResultItem,
  DownloadImportSourceType,
  MagnetProbePayload,
} from '../types'

const DOWNLOAD_IMPORT_TERMINAL_PROBE_STATUSES = new Set<MagnetProbePayload['status']>(['complete', 'failed', 'cancelled'])

function downloadImportProbeIsTrustedTerminal(probe: MagnetProbePayload | null | undefined): boolean {
  return Boolean(
    probe
    && DOWNLOAD_IMPORT_TERMINAL_PROBE_STATUSES.has(probe.status)
    && (probe.cleanup?.status === 'complete' || probe.cleanup?.status === 'not_required'),
  )
}

const downloadImportSourceLabels: Record<DownloadImportSourceType, string> = {
  magnet: '磁链',
  thunder: '迅雷链接',
  btih: 'BTIH 哈希',
}

function mergeDownloadImportProbe(
  preview: DownloadImportPreviewPayload,
  probe: MagnetProbePayload | null,
): DownloadImportPreviewPayload {
  if (!probe?.items.length) return preview
  const inspectedByHash = new Map(probe.items.map((item) => [item.info_hash, item]))
  const items = preview.items.map((item) => {
    const inspected = inspectedByHash.get(item.info_hash)
    if (!inspected?.metadata_status) return item
    const catalogCode = item.catalog_code || inspected.catalog_code || null
    const torrentName = inspected.torrent_name || item.torrent_name || null
    return {
      ...item,
      display_name: torrentName || item.display_name,
      catalog_code: catalogCode,
      requires_confirmation: !catalogCode,
      metadata_status: inspected.metadata_status,
      torrent_name: torrentName,
      total_size: inspected.total_size ?? item.total_size ?? null,
      file_count: inspected.file_count ?? item.file_count ?? null,
      files: inspected.files ?? item.files ?? [],
      files_truncated: inspected.files_truncated ?? item.files_truncated ?? false,
      content_error: inspected.content_error ?? null,
    }
  })
  return {
    ...preview,
    items,
    requires_confirmation: items.some((item) => item.requires_confirmation),
  }
}

function torrentImportMetadataStatusLabel(status: DownloadImportMetadataStatus): string {
  if (status === 'pending') return '正在获取 torrent 元数据'
  if (status === 'ready') return '元数据已获取'
  if (status === 'unavailable') return '暂未获取到 torrent 元数据'
  if (status === 'restricted') return '已有任务不在 JAV 分类，未读取内容'
  return '尚未读取内容'
}

function TorrentImportFileDetails({
  item,
  defaultOpen,
}: {
  item: DownloadImportPreviewItem | DownloadImportResultItem
  defaultOpen: boolean
}) {
  const [expanded, setExpanded] = useState(defaultOpen)
  const files = item.files ?? []
  const fileCount = item.file_count ?? files.length
  const summary = item.files_truncated
    ? files.length
      ? `查看已读取的 ${files.length} / ${fileCount} 个文件`
      : `文件清单未展示，共 ${fileCount} 个文件`
    : `查看 ${fileCount} 个文件`

  return (
    <details
      className="torrent-import-file-details"
      open={expanded}
      onToggle={(event) => setExpanded(event.currentTarget.open)}
    >
      <summary>{summary}</summary>
      {files.length ? (
        <ul>
          {files.map((file) => (
            <li key={`${file.index}-${file.name}`}>
              <span>{file.name}</span>
              <span>{formatBytes(file.size)}</span>
            </li>
          ))}
        </ul>
      ) : null}
      {item.files_truncated ? (
        <span className="torrent-import-files-truncated">
          {files.length
            ? `文件较多，仅显示前 ${files.length} 个，共 ${fileCount} 个。`
            : `文件较多，本次未展示清单，共 ${fileCount} 个。`}
        </span>
      ) : null}
    </details>
  )
}

function TorrentImportItems({
  items,
  label,
  inspectionPending = false,
}: {
  items: readonly (DownloadImportPreviewItem | DownloadImportResultItem)[]
  label: string
  inspectionPending?: boolean
}) {
  if (!items.length) return null

  return (
    <ul className="torrent-import-items" aria-label={label}>
      {items.map((item) => {
        const result = 'status' in item ? item : null
        const title = item.torrent_name || item.display_name || item.catalog_code || item.info_hash
        const metadataStatus = item.metadata_status ?? (inspectionPending ? 'pending' : 'not_requested')
        const showFileDetails = metadataStatus === 'ready' && item.file_count !== null && item.file_count !== undefined
        return (
          <li className={result ? `torrent-import-item import-${result.status}` : 'torrent-import-item'} key={item.info_hash}>
            <div className="torrent-import-item-main">
              <strong title={title}>{title}</strong>
              <code title={item.info_hash}>{item.info_hash}</code>
              <div className={`torrent-import-item-content-meta metadata-${metadataStatus}`}>
                {item.total_size !== null && item.total_size !== undefined ? <span>{formatBytes(item.total_size)}</span> : null}
                {item.file_count !== null && item.file_count !== undefined ? <span>{item.file_count} 个文件</span> : null}
                <span>{torrentImportMetadataStatusLabel(metadataStatus)}</span>
              </div>
              {result?.error ? <span className="torrent-import-item-error">{result.error}</span> : null}
              {result?.metadata_warning ? <span className="torrent-import-item-warning">{result.metadata_warning}</span> : null}
              {item.content_error ? <span className="torrent-import-item-warning">文件清单读取失败：{item.content_error}</span> : null}
            </div>
            <div className="torrent-import-item-meta">
              <StatusBadge>{downloadImportSourceLabels[item.source_type]}</StatusBadge>
              {item.catalog_code ? <StatusBadge tone="success">{item.catalog_code}</StatusBadge> : null}
              {item.requires_confirmation && metadataStatus !== 'pending' ? <StatusBadge tone="warning">未从名称或文件中识别到番号</StatusBadge> : null}
              {result ? (
                <StatusBadge tone={result.status === 'added' ? 'success' : 'danger'}>
                  {result.status === 'added' ? '已添加' : '添加失败'}
                </StatusBadge>
              ) : null}
            </div>
            {showFileDetails ? <TorrentImportFileDetails item={item} defaultOpen={items.length === 1} /> : null}
          </li>
        )
      })}
    </ul>
  )
}

function TorrentImportErrors({
  errors,
  invalidCount = errors.length,
}: {
  errors: readonly DownloadImportError[]
  invalidCount?: number
}) {
  if (!invalidCount && !errors.length) return null

  return (
    <div className="torrent-import-errors" role="alert">
      <strong>{invalidCount || errors.length} 条输入无法解析</strong>
      {errors.length ? (
        <ul>
          {errors.map((item, index) => (
            <li key={`${item.input}-${index}`}>
              <code title={item.input}>{item.input}</code>
              <span>{downloadImportErrorMessage(item.error)}</span>
            </li>
          ))}
        </ul>
      ) : <span>这些输入未创建下载任务，请修改后重新解析。</span>}
    </div>
  )
}

function downloadImportErrorMessage(error: string): string {
  const normalized = error.trim().toLowerCase()
  if (normalized.includes('thunder uri target must be a magnet uri or btih hash')) {
    return '迅雷链接解码后不是 BT 磁链或 BTIH 哈希'
  }
  if (normalized.includes('download input')) {
    return '输入不是有效的磁链、迅雷链接或 BTIH 哈希'
  }
  if (normalized.includes('thunder uri')) return '迅雷链接格式无效'
  if (normalized.includes('magnet uri')) return '磁链格式无效'
  if (normalized.includes('btih')) {
    return '输入不是有效的磁链、迅雷链接或 BTIH 哈希'
  }
  return error || '输入无法解析'
}

export function TorrentImportTool({
  downloaderOnline,
  downloaderChecking,
}: {
  downloaderOnline: boolean
  downloaderChecking: boolean
}) {
  const toast = useToast()
  const queryClient = useQueryClient()
  const [input, setInput] = useState('')
  const [fieldError, setFieldError] = useState('')
  const [preview, setPreview] = useState<DownloadImportPreviewPayload | null>(null)
  const [confirmUnrecognized, setConfirmUnrecognized] = useState(false)
  const [inspectionProbeId, setInspectionProbeId] = useState('')
  const [inspectionSeed, setInspectionSeed] = useState<MagnetProbePayload | null>(null)
  const [inspectionStarting, setInspectionStarting] = useState(false)
  const [inspectionError, setInspectionError] = useState('')
  const [outcome, setOutcome] = useState<{
    payload: DownloadImportPayload
    previewErrors: DownloadImportError[]
  } | null>(null)
  const inputRef = useRef(input)
  const inspectionGenerationRef = useRef(0)
  inputRef.current = input

  const inspectImport = useMutation({
    mutationFn: (normalizedInput: string) => api.inspectDownloadImport(normalizedInput),
  })
  const previewImport = useMutation({
    mutationFn: (normalizedInput: string) => api.previewDownloadImport(normalizedInput),
    onSuccess: (payload) => {
      setPreview(payload)
      setOutcome(null)
      setConfirmUnrecognized(false)
      setFieldError('')
    },
  })
  const submitImport = useMutation({
    mutationFn: ({
      normalizedInput,
      confirmed,
      probeId,
    }: {
      normalizedInput: string
      confirmed: boolean
      probeId?: string
      previewErrors: DownloadImportError[]
    }) => probeId
      ? api.submitDownloadImport(normalizedInput, confirmed, probeId)
      : api.submitDownloadImport(normalizedInput, confirmed),
    onSuccess: (payload, variables) => {
      setOutcome({ payload, previewErrors: variables.previewErrors })
      setPreview(null)
      setConfirmUnrecognized(false)
      resetInspection()
      if (payload.failed_count === 0 && payload.invalid_count === 0) {
        inputRef.current = ''
        setInput('')
      }
      if (payload.added_count > 0) void queryClient.invalidateQueries({ queryKey: ['downloads'] })

      const message = payload.failed_count || payload.invalid_count
        ? `已添加 ${payload.added_count} 个任务，${payload.failed_count} 个失败，${payload.invalid_count} 条无效`
        : `已添加 ${payload.added_count} 个 BT 下载任务`
      toast.push(message, payload.failed_count || payload.invalid_count ? (payload.added_count ? 'info' : 'error') : 'success')
    },
  })

  const inspectionQuery = useQuery({
    queryKey: ['download-import-inspection', inspectionProbeId],
    queryFn: () => api.magnetProbe(inspectionProbeId),
    enabled: Boolean(inspectionProbeId),
    refetchInterval: (query) => {
      const payload = query.state.data as MagnetProbePayload | undefined
      if (query.state.error instanceof ApiError && query.state.error.status === 404) return false
      return payload && DOWNLOAD_IMPORT_TERMINAL_PROBE_STATUSES.has(payload.status) ? false : 750
    },
    refetchIntervalInBackground: false,
    retry: false,
  })

  const busy = previewImport.isPending || submitImport.isPending
  const inspectionProbe = inspectionProbeId ? inspectionQuery.data ?? inspectionSeed : null
  const inspectionTerminal = Boolean(
    inspectionProbe && DOWNLOAD_IMPORT_TERMINAL_PROBE_STATUSES.has(inspectionProbe.status),
  )
  const inspectionStateLost = Boolean(
    inspectionProbeId && inspectionQuery.error instanceof ApiError && inspectionQuery.error.status === 404,
  )
  const inspectionTrustedTerminal = !inspectionQuery.error && downloadImportProbeIsTrustedTerminal(inspectionProbe)
  const inspectionUnresolved = inspectionStarting || Boolean(inspectionProbeId && !inspectionTerminal && !inspectionStateLost)
  const effectivePreview = preview ? mergeDownloadImportProbe(preview, inspectionProbe) : null
  const inspectionCleanupBlocked = Boolean(inspectionProbeId && inspectionTerminal && !inspectionTrustedTerminal)
  const probeIdForSubmit = inspectionProbeId && inspectionTrustedTerminal ? inspectionProbeId : undefined
  const unrecognizedCount = effectivePreview?.items.filter((item) => item.requires_confirmation).length ?? 0
  const canSubmit = Boolean(
    effectivePreview?.items.length
    && downloaderOnline
    && !inspectionUnresolved
    && !inspectionCleanupBlocked
    && !inspectionStateLost
    && (!effectivePreview.requires_confirmation || confirmUnrecognized),
  )
  const inspectionButtonLabel = inspectionStateLost
    ? '记录已过期'
    : inspectionCleanupBlocked
    ? '清理未完成'
    : inspectionUnresolved
      ? inspectionQuery.error ? '等待安全清理' : '正在读取内容'
      : '读取内容'

  let inspectionNotice: { tone: 'info' | 'success' | 'warning'; message: string } | null = null
  if (inspectionStarting) {
    inspectionNotice = { tone: 'info', message: '正在启动内容读取。' }
  } else if (inspectionStateLost) {
    inspectionNotice = {
      tone: 'warning',
      message: '读取记录已过期，当前资源不可直接提交；可清除预览后重新解析。',
    }
  } else if (inspectionCleanupBlocked) {
    inspectionNotice = {
      tone: 'warning',
      message: '临时探测任务清理未完成，当前资源不可提交；可清除预览后处理其他输入。',
    }
  } else if (inspectionProbeId && inspectionQuery.error) {
    inspectionNotice = {
      tone: 'warning',
      message: `内容读取状态暂不可用，仍在等待安全清理：${(inspectionQuery.error as Error).message || '探测状态不可用'}。`,
    }
  } else if (inspectionProbe) {
    if (inspectionProbe.status === 'queued') inspectionNotice = { tone: 'info', message: '内容读取已排队。' }
    if (inspectionProbe.status === 'running') {
      inspectionNotice = {
        tone: 'info',
        message: `正在读取内容，已完成 ${inspectionProbe.progress.resolved} / ${inspectionProbe.progress.total} 条。`,
      }
    }
    if (inspectionProbe.status === 'cleaning') {
      inspectionNotice = { tone: 'info', message: '内容读取已结束，正在清理临时任务。' }
    }
    if (inspectionProbe.status === 'cancelling') {
      inspectionNotice = { tone: 'warning', message: '正在停止内容读取。' }
    }
    if (inspectionProbe.status === 'complete') {
      const readyCount = inspectionProbe.items.filter((item) => item.metadata_status === 'ready').length
      inspectionNotice = readyCount === inspectionProbe.total
        ? { tone: 'success', message: `已读取 ${readyCount} 条资源的 torrent 内容。` }
        : { tone: 'warning', message: `内容读取完成，${readyCount} / ${inspectionProbe.total} 条获取到 torrent 元数据。` }
    }
    if (inspectionProbe.status === 'failed' || inspectionProbe.status === 'cancelled') {
      const detail = inspectionProbe.error ? `：${inspectionProbe.error}` : ''
      inspectionNotice = { tone: 'warning', message: `内容读取未完成${detail}。可按当前预览继续确认下载。` }
    }
  } else if (inspectionError) {
    inspectionNotice = { tone: 'warning', message: `无法读取内容：${inspectionError}。可按当前预览继续确认下载。` }
  }

  function resetInspection() {
    inspectionGenerationRef.current += 1
    setInspectionProbeId('')
    setInspectionSeed(null)
    setInspectionStarting(false)
    setInspectionError('')
  }

  function updateInput(value: string) {
    if (inspectionUnresolved) return
    inputRef.current = value
    setInput(value)
    setFieldError('')
    setPreview(null)
    setOutcome(null)
    setConfirmUnrecognized(false)
    resetInspection()
    previewImport.reset()
    submitImport.reset()
  }

  function parseInput(event: FormEvent<HTMLFormElement>) {
    event.preventDefault()
    if (inspectionUnresolved || inspectionCleanupBlocked || inspectionStateLost) return
    const normalizedInput = input.trim()
    if (!normalizedInput) {
      setFieldError('请输入至少一条迅雷链接、磁链或 BTIH 哈希')
      return
    }
    inputRef.current = normalizedInput
    setInput(normalizedInput)
    setPreview(null)
    setOutcome(null)
    setConfirmUnrecognized(false)
    resetInspection()
    previewImport.reset()
    submitImport.reset()
    previewImport.mutate(normalizedInput)
  }

  async function inspectPreview() {
    const normalizedInput = input.trim()
    if (!preview?.items.length || !normalizedInput || !downloaderOnline || inspectionUnresolved || inspectionCleanupBlocked || inspectionStateLost) return
    const requestGeneration = inspectionGenerationRef.current + 1
    inspectionGenerationRef.current = requestGeneration
    const requestIsCurrent = () => inspectionGenerationRef.current === requestGeneration
      && inputRef.current === normalizedInput
    setInspectionProbeId('')
    setInspectionSeed(null)
    setInspectionStarting(true)
    setInspectionError('')
    setConfirmUnrecognized(false)
    try {
      const payload = await inspectImport.mutateAsync(normalizedInput)
      if (!requestIsCurrent()) return
      setPreview(payload)
      setInspectionSeed(payload.probe)
      setInspectionProbeId(payload.probe.probe_id)
    } catch (error) {
      if (!requestIsCurrent()) return
      setInspectionError((error as Error).message || '无法启动 torrent 内容读取')
    } finally {
      if (requestIsCurrent()) setInspectionStarting(false)
    }
  }

  function submitPreview() {
    if (!effectivePreview?.items.length || !downloaderOnline || inspectionUnresolved || inspectionCleanupBlocked || inspectionStateLost) return
    if (effectivePreview.requires_confirmation && !confirmUnrecognized) return
    submitImport.reset()
    submitImport.mutate({
      normalizedInput: input,
      confirmed: effectivePreview.requires_confirmation ? confirmUnrecognized : false,
      probeId: probeIdForSubmit,
      previewErrors: [...effectivePreview.errors],
    })
  }

  const outcomeTone = outcome
    ? outcome.payload.failed_count || outcome.payload.invalid_count
      ? outcome.payload.added_count > 0 ? 'warning' as const : 'danger' as const
      : 'success' as const
    : 'neutral' as const

  return (
    <section className="torrent-import-tool" aria-labelledby="torrent-import-title">
      <div className="torrent-import-heading">
        <div>
          <h2 id="torrent-import-title">添加 BT 下载</h2>
          <p>批量粘贴迅雷链接、磁链或 BTIH 哈希，确认后加入 qBittorrent。</p>
        </div>
        {effectivePreview ? <StatusBadge tone={effectivePreview.requires_confirmation && !inspectionUnresolved ? 'warning' : 'info'}>{effectivePreview.count} 条待下载</StatusBadge> : null}
        {outcome ? <StatusBadge tone={outcomeTone}>{outcome.payload.added_count} 条已添加</StatusBadge> : null}
      </div>

      <form className="torrent-import-form" onSubmit={parseInput} noValidate>
        <label>
          <span>迅雷链接、磁链或 BTIH 哈希</span>
          <textarea
            rows={4}
            value={input}
            placeholder={'每行一条：磁链、迅雷链接或 40 位 info hash'}
            autoComplete="off"
            autoCapitalize="none"
            spellCheck={false}
            aria-invalid={Boolean(fieldError)}
            aria-describedby={`torrent-import-hint${fieldError ? ' torrent-import-field-error' : ''}`}
            disabled={busy || inspectionUnresolved}
            onChange={(event) => updateInput(event.target.value)}
          />
        </label>
        <div className="torrent-import-form-footer">
          <span id="torrent-import-hint">每行一个；重复资源会按 info hash 合并，解析不会创建下载任务。</span>
          <Button type="submit" variant="secondary" disabled={busy || inspectionUnresolved || inspectionCleanupBlocked || inspectionStateLost}>
            <ScanSearch className={previewImport.isPending ? 'spin' : ''} aria-hidden="true" />
            {previewImport.isPending ? '正在解析' : '解析预览'}
          </Button>
        </div>
        {fieldError ? <span className="torrent-import-field-error" id="torrent-import-field-error" role="alert">{fieldError}</span> : null}
      </form>

      {previewImport.error ? (
        <InlineNotice tone="danger" role="alert">{(previewImport.error as Error).message || '无法解析输入，请重试。'}</InlineNotice>
      ) : null}
      {submitImport.error ? (
        <InlineNotice tone="danger" role="alert">{(submitImport.error as Error).message || '无法添加下载任务，请重试。'}</InlineNotice>
      ) : null}

      {effectivePreview ? (
        <div className="torrent-import-preview">
          <div className="torrent-import-summary" role="status" aria-live="polite" aria-atomic="true">
            <div>
              <strong>{effectivePreview.count ? `解析到 ${effectivePreview.count} 条可下载资源` : '未解析到可下载资源'}</strong>
              <span>
                {inspectionUnresolved
                  ? '正在读取 torrent 名称、大小与文件清单。'
                  : effectivePreview.errors.length
                    ? `${effectivePreview.errors.length} 条输入无法解析，可继续下载其余有效资源。`
                    : '检查名称、大小与番号后再确认下载。'}
              </span>
            </div>
            <div>
              {effectivePreview.duplicate_count ? <StatusBadge>{effectivePreview.duplicate_count} 条重复已合并</StatusBadge> : null}
              {!inspectionUnresolved && unrecognizedCount ? <StatusBadge tone="warning">{unrecognizedCount} 条未识别番号</StatusBadge> : null}
            </div>
          </div>

          {inspectionNotice ? <InlineNotice tone={inspectionNotice.tone} role="status">{inspectionNotice.message}</InlineNotice> : null}

          <TorrentImportItems items={effectivePreview.items} label="BT 下载导入预览" inspectionPending={inspectionUnresolved} />
          <TorrentImportErrors errors={effectivePreview.errors} />

          {effectivePreview.requires_confirmation && !inspectionUnresolved && !inspectionCleanupBlocked && !inspectionStateLost ? (
            <InlineNotice tone="warning" role="alert">
              <div className="torrent-import-confirmation">
                <div>
                  <strong>包含未识别番号的资源</strong>
                  <span id="torrent-import-confirmation-description">
                    {unrecognizedCount
                      ? `其中 ${unrecognizedCount} 条未从名称或文件中识别到番号，确认前不会提交任何任务。`
                      : '部分资源未从名称或文件中识别到番号，确认前不会提交任何任务。'}
                  </span>
                </div>
                <Toggle
                  label={unrecognizedCount
                    ? `我确认仍下载 ${unrecognizedCount} 条未识别番号的资源`
                    : '我确认仍下载这些未识别番号的资源'}
                  checked={confirmUnrecognized}
                  aria-describedby="torrent-import-confirmation-description"
                  disabled={submitImport.isPending}
                  onChange={(event) => setConfirmUnrecognized(event.target.checked)}
                />
              </div>
            </InlineNotice>
          ) : null}

          {!downloaderOnline ? (
            <InlineNotice tone="warning" role="status">
              {downloaderChecking
                ? '正在检查 qBittorrent；预览会保留，连接确认后即可提交。'
                : 'qBittorrent 当前不可用；预览会保留，恢复连接后才能提交。'}
            </InlineNotice>
          ) : null}

          <div className="torrent-import-actions">
            <Button
              type="button"
              className="torrent-import-inspect-button"
              variant="secondary"
              disabled={!downloaderOnline || inspectionUnresolved || inspectionCleanupBlocked || inspectionStateLost || submitImport.isPending}
              onClick={() => void inspectPreview()}
            >
              <ScanSearch className={inspectionUnresolved ? 'spin' : ''} aria-hidden="true" />
              {inspectionButtonLabel}
            </Button>
            <Button
              type="button"
              className="torrent-import-submit-button"
              variant="primary"
              disabled={!canSubmit || submitImport.isPending}
              onClick={submitPreview}
            >
              <Download aria-hidden="true" />
              {submitImport.isPending ? '正在加入' : `确认下载 ${effectivePreview.count} 个任务`}
            </Button>
            <Button
              type="button"
              className="torrent-import-clear-button"
              size="small"
              variant="ghost"
              disabled={submitImport.isPending || inspectionUnresolved}
              onClick={() => {
                if (inspectionUnresolved) return
                setPreview(null)
                setConfirmUnrecognized(false)
                resetInspection()
              }}
            >
              <X aria-hidden="true" />
              清除预览
            </Button>
          </div>
        </div>
      ) : null}

      {outcome ? (
        <div className="torrent-import-result">
          <div className="torrent-import-result-heading" role="status" aria-live="polite" aria-atomic="true">
            <strong>
              {outcome.payload.failed_count || outcome.payload.invalid_count
                ? `批量处理完成：已添加 ${outcome.payload.added_count}，失败 ${outcome.payload.failed_count}，无效 ${outcome.payload.invalid_count}`
                : `已添加 ${outcome.payload.added_count} 个 BT 下载任务`}
            </strong>
            <span>
              {outcome.payload.failed_count || outcome.payload.invalid_count
                ? '原输入已保留；修改失败或无效项后可重新解析。'
                : '任务列表会自动刷新。'}
            </span>
          </div>
          <TorrentImportItems items={outcome.payload.items} label="BT 下载导入结果" />
          <TorrentImportErrors errors={outcome.previewErrors} invalidCount={outcome.payload.invalid_count} />
        </div>
      ) : null}
    </section>
  )
}
