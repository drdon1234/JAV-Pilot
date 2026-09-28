import { currentLocale, t } from './i18n'

export function formatBytes(value: number, perSecond = false): string {
  const suffix = perSecond ? '/s' : ''
  if (!Number.isFinite(value) || value <= 0) return `0 B${suffix}`
  const units = ['B', 'KB', 'MB', 'GB', 'TB']
  const index = Math.min(Math.floor(Math.log(value) / Math.log(1024)), units.length - 1)
  const amount = value / 1024 ** index
  const digits = amount >= 100 || index === 0 ? 0 : amount >= 10 ? 1 : 2
  return `${amount.toFixed(digits)} ${units[index]}${suffix}`
}

export function formatEta(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return t('未知')
  if (seconds === 0) return t('已完成')
  const days = Math.floor(seconds / 86400)
  const hours = Math.floor((seconds % 86400) / 3600)
  const minutes = Math.floor((seconds % 3600) / 60)
  if (days) return t('{days}天 {hours}小时', { days, hours })
  if (hours) return t('{hours}小时 {minutes}分钟', { hours, minutes })
  return t('{value}分钟', { value: Math.max(1, minutes) })
}

export function formatDateTime(timestamp: number): string {
  if (!timestamp) return t('未知')
  return new Intl.DateTimeFormat(currentLocale(), {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  }).format(new Date(timestamp * 1000))
}

export function splitTerms(value: string): string[] {
  return value
    .split(/[,，\n]/)
    .map((item) => item.trim())
    .filter(Boolean)
    .slice(0, 20)
}

/** An absolute http(s) link that is safe to open from scraped data, or null. */
export function externalHttpUrl(value: string | null | undefined): string | null {
  if (!value) return null
  try {
    const url = new URL(value)
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null
    if (url.username || url.password) return null
    return url.href
  } catch {
    return null
  }
}
