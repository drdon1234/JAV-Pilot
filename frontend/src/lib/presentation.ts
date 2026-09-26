const ERROR_CODE_LABELS: Record<string, string> = {
  artwork_unavailable: '图片资源暂不可用',
  busy: '诊断任务正忙，请稍后重试',
  challenge: '站点验证尚未完成',
  challenge_active: '站点验证尚未完成',
  challenge_detected: '站点验证尚未完成',
  code_mismatch: '验收番号与页面内容不匹配',
  configuration: '站点配置无效',
  configuration_invalid: '站点配置无效',
  connection_failed: '无法连接站点',
  continuation_in_progress: '续搜任务仍在进行',
  continuation_invalid: '续搜位置已失效',
  dependency_unavailable: '所需服务暂不可用',
  dispatcher_crash: '通知调度意外中断',
  discovery_unavailable: '资源页面暂时无法解析',
  dns_failed: '站点域名解析失败',
  download_failed: '下载任务失败',
  image_host_rejected: '图片地址不受信任',
  image_invalid: '图片响应无效',
  internal_error: '服务内部处理失败',
  internal_failure: '服务内部处理失败',
  invalid_config: '站点配置无效',
  invalid_instruction: '请求条件无效',
  media_transport_interrupted: '媒体传输意外中断',
  media_host_rejected: '媒体地址不受信任',
  manifest_invalid: '媒体清单无效',
  metadata_artwork_unavailable: '元数据图片暂不可用',
  no_result: '未找到匹配结果',
  parse_drift: '站点页面结构可能已变化',
  rate_limited: '站点请求过于频繁',
  range_unsupported: '媒体服务器不支持分段读取',
  redirect_rejected: '重定向目标不受信任',
  response_too_large: '站点响应超过安全限制',
  side_effect_detected: '诊断请求触发了非预期操作',
  snapshot_failed: '诊断快照保存失败',
  source_unavailable: '资源站点暂不可用',
  stream_invalid: '搜索数据流格式无效',
  stream_too_large: '搜索数据流超过安全限制',
  timeout: '站点响应超时',
  tls_failed: '站点安全连接失败',
  transient_browser_failure: '页面访问暂时失败',
  transport_failed: '通知通道连接失败',
  upstream_http: '上游站点响应异常',
  upstream_rate_limited: '上游站点请求过于频繁',
  upstream_timeout: '上游站点响应超时',
}

const PARSE_STATUS_LABELS: Record<string, string> = {
  complete: '解析完成',
  error: '解析失败',
  partial: '部分解析',
  pending: '等待解析',
  resolved: '详情已解析',
  summary: '仅摘要',
}

const DIAGNOSTIC_STAGE_LABELS: Record<string, string> = {
  configuration: '配置',
  connection: '连接',
  detail: '详情',
  dns: 'DNS',
  image: '图片',
  manifest: '媒体清单',
  quality: '画质',
  search: '搜索',
}

const CHINESE_TEXT_PATTERN = /[\u3400-\u9fff]/
const UNSAFE_MESSAGE_PATTERN = /[\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f]|\r|\n|\btraceback\b|\bat\s+\S+\s*\(/i

function isSafeChineseMessage(message: string): boolean {
  return Boolean(
    message
    && message.length <= 240
    && CHINESE_TEXT_PATTERN.test(message)
    && !UNSAFE_MESSAGE_PATTERN.test(message)
  )
}

export function errorCodeLabel(code: string | null | undefined, fallback = '处理失败'): string {
  const cleanCode = code?.trim().toLowerCase() ?? ''
  return cleanCode ? ERROR_CODE_LABELS[cleanCode] ?? fallback : fallback
}

export function parseStatusLabel(status: string): string {
  const cleanStatus = status.trim().toLowerCase()
  return PARSE_STATUS_LABELS[cleanStatus] ?? '状态未知'
}

export function diagnosticStageLabel(stage: string | null | undefined, fallback = '站点'): string {
  const cleanStage = stage?.trim().toLowerCase() ?? ''
  return cleanStage ? DIAGNOSTIC_STAGE_LABELS[cleanStage] ?? fallback : fallback
}

// Backend messages are English and technical. The ones a user can act on are
// translated here so a failure names its cause instead of a generic fallback.
const SERVICE_MESSAGE_LABELS: Array<[RegExp, string]> = [
  [/^media library index is (?:unavailable|disabled)$/i, '媒体库索引尚未就绪，请稍后重试，或在“媒体库”页重建索引'],
  [/^media library (?:root|path) is unavailable$/i, '媒体库目录当前不可访问，请检查挂载'],
  [/^media library could not be inspected/i, '暂时无法检查媒体库中的已有作品，请稍后重试'],
  [/^media library (?:metadata could not be inspected|changed)/i, '媒体库正在更新，请稍后重试'],
  [/^completed downloads could not be inspected$/i, '暂时无法读取已完成的下载记录，请稍后重试'],
  [/^no web download (?:site|provider) is enabled$/i, '没有启用的 Web 下载站点，请在“站点”中启用'],
  [/^web downloads? (?:are|is) disabled$/i, 'Web 下载功能未启用'],
  [/^web downloads are unavailable in maintenance mode$/i, '维护模式下暂停 Web 下载'],
  [/^web download storage is unavailable$/i, 'Web 下载存储暂不可用，请检查下载目录'],
  [/^web download staging path is unsafe$/i, 'Web 下载暂存目录不安全，请检查配置'],
  [/^resource search storage is unavailable$/i, '资源搜索记录暂时无法读取'],
  [/^resource search (?:source|site configuration) is (?:unavailable|invalid)$/i, '资源站点未启用或配置无效'],
  [/^resource search revision changed$/i, '搜索结果已更新，请重新选择后再提交'],
  [/^(?:server|web download batch manager) is shutting down$/i, '服务正在重启，请稍后重试'],
  [/^batch rule revision is stale$/i, '批量规则已被修改，请重新载入规则'],
  [/^too many selected catalog codes$/i, '一次选择的作品过多，请分批提交'],
  [/^metadata (?:sources|details) are temporarily unavailable$/i, '元数据站点暂时无法访问，稍后会自动重试'],
  [/^media metadata is disabled$/i, '元数据功能未启用'],
  [/^qbittorrent is not configured$/i, '尚未配置 qBittorrent'],
  [/^too many active searches$/i, '同时进行的搜索过多，请稍后再试'],
]

export function translateServiceMessage(message: string): string | null {
  const clean = message.trim()
  if (!clean) return null
  return SERVICE_MESSAGE_LABELS.find(([pattern]) => pattern.test(clean))?.[1] ?? null
}

export function serviceErrorMessage(
  error: unknown,
  fallback = '操作未完成，请稍后重试',
): string {
  const candidate = typeof error === 'string'
    ? error
    : error && typeof error === 'object' && 'message' in error
      ? String((error as { message?: unknown }).message ?? '')
      : ''
  const code = error && typeof error === 'object' && 'code' in error
    ? String((error as { code?: unknown }).code ?? '')
    : ''
  const message = candidate.trim()
  if (code) {
    const cleanCode = code.trim().toLowerCase()
    const knownLabel = ERROR_CODE_LABELS[cleanCode]
    if (knownLabel) return knownLabel
  }
  if (isSafeChineseMessage(message)) return message
  return translateServiceMessage(message) ?? fallback
}
