import type { OrganizerRule } from '../types'

export function summarizeRule(rule: OrganizerRule) {
  const scope = rule.match.sources?.length ? `${rule.match.sources.length} 个来源` : '全部来源'
  const conditions: string[] = []
  if (rule.match.title_contains?.length) conditions.push(`标题关键词 ${rule.match.title_contains.length} 项`)
  if (rule.match.magnet_name_contains?.length) conditions.push(`磁链关键词 ${rule.match.magnet_name_contains.length} 项`)
  if (rule.match.code_regex) conditions.push('番号规则')
  const match = conditions.length ? conditions.join('、') : '所有任务'
  const category = rule.actions.category ? `分类 ${rule.actions.category}` : '当前 JAV 分类'
  const savePath = rule.actions.save_path || '当前 JAV 暂存根'
  const destination = `${category} / ${savePath}`
  return `${scope} · ${match} → ${destination}`
}
