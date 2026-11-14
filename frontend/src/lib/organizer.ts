import type { OrganizerRule } from '../types'
import { t } from './i18n'

export function moveOrganizerRule(rules: OrganizerRule[], index: number, direction: -1 | 1): OrganizerRule[] {
  const target = index + direction
  if (index < 0 || index >= rules.length || target < 0 || target >= rules.length) return rules
  const reordered = [...rules]
  const [rule] = reordered.splice(index, 1)
  reordered.splice(target, 0, rule)
  // The backend matches numeric priority, not JSON array order. Keep the
  // visible move and the effective matching order as one operation.
  return reordered.map((item, position) => ({ ...item, priority: (position + 1) * 100 }))
}

export function summarizeRule(rule: OrganizerRule) {
  const scope = rule.match.sources?.length ? t('{count} 个来源', { count: rule.match.sources.length }) : t('全部来源')
  const conditions: string[] = []
  if (rule.match.title_contains?.length) conditions.push(t('标题关键词 {count} 项', { count: rule.match.title_contains.length }))
  if (rule.match.magnet_name_contains?.length) conditions.push(t('磁链关键词 {count} 项', { count: rule.match.magnet_name_contains.length }))
  if (rule.match.code_regex) conditions.push(t('番号规则'))
  const match = conditions.length ? conditions.join(t('、')) : t('所有任务')
  const category = rule.actions.category ? t('分类 {category}', { category: rule.actions.category }) : t('当前 JAV 分类')
  const savePath = rule.actions.save_path || t('当前 JAV 暂存根')
  const destination = `${category} / ${savePath}`
  return `${scope} · ${match} → ${destination}`
}
