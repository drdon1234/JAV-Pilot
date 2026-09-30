import type { SubtitleJobStatus, SubtitleScript } from '../types'
import { t } from './i18n'

export const SUBTITLE_ACTIVE_STATUSES = new Set<SubtitleJobStatus>(['queued', 'running', 'retry'])

export const SUBTITLE_STATUS_FILTERS = [
  { value: 'all', label: t('全部状态') },
  { value: 'waiting', label: t('等待中') },
  { value: 'running', label: t('获取中') },
  { value: 'completed', label: t('已完成') },
  { value: 'not_found', label: t('未找到') },
  { value: 'skipped', label: t('已跳过') },
  { value: 'failed', label: t('失败') },
] as const

export const PROVIDER_LABELS: Record<string, string> = {
  xunlei: t('迅雷字幕'),
  subtitlecat: 'SubtitleCat',
}

const STATUS_LABELS: Record<SubtitleJobStatus, string> = {
  queued: t('排队中'),
  running: t('获取中'),
  retry: t('等待重试'),
  completed: t('已完成'),
  not_found: t('未找到'),
  existing: t('已有字幕'),
  skipped: t('已跳过'),
  failed: t('失败'),
  removed: t('已删除'),
}

const REASON_LABELS: Record<string, string> = {
  no_candidates: t('字幕来源中没有这部作品'),
  no_usable_candidate: t('找到的字幕都不可用'),
  hardsub: t('中文字幕版本已内嵌字幕'),
  multipart: t('分段影片暂不支持'),
  external_subtitle: t('影片旁已有其他字幕'),
  media_missing: t('影片文件不存在'),
  no_provider: t('没有启用的字幕来源'),
  provider_unavailable: t('字幕来源暂不可用'),
  library_unavailable: t('媒体库暂不可用'),
  file_conflict: t('同名字幕文件已存在'),
  file_modified: t('字幕文件已被手动修改'),
}

const REJECTION_LABELS: Record<string, string> = {
  code_mismatch: t('番号不符'),
  format_unsupported: t('格式不支持'),
  machine_translation_disabled: t('已关闭机翻字幕'),
  duration_mismatch: t('时长不符'),
  not_chinese: t('不是中文'),
  timeline_too_long: t('时间轴超出影片'),
  too_few_cues: t('内容过少'),
  format_unknown: t('无法识别格式'),
  undecodable: t('编码无法识别'),
  empty: t('文件为空'),
  too_large: t('文件过大'),
  download_failed: t('下载失败'),
}

export function subtitleStatusLabel(status: SubtitleJobStatus): string {
  return STATUS_LABELS[status] ?? t('状态未知')
}

export function subtitleStatusTone(status: SubtitleJobStatus) {
  if (status === 'completed') return 'success' as const
  if (status === 'failed') return 'danger' as const
  if (status === 'running') return 'info' as const
  if (status === 'retry' || status === 'not_found') return 'warning' as const
  return 'neutral' as const
}

export function subtitleReasonLabel(reason: string | null): string | null {
  if (!reason) return null
  return REASON_LABELS[reason] ?? t('处理失败')
}

export function subtitleRejectionLabel(reason: string): string {
  return REJECTION_LABELS[reason] ?? t('不可用')
}

export function subtitleScriptLabel(script: SubtitleScript | null): string {
  if (script === 'zh-CN') return t('简体')
  if (script === 'zh-TW') return t('繁体')
  return t('字形未声明')
}

export function durationDeltaLabel(deltaMs: number | null): string | null {
  if (deltaMs === null) return null
  const minutes = Math.round(deltaMs / 60_000)
  return minutes === 0 ? t('时长吻合') : t('时长相差 {minutes} 分钟', { minutes })
}
