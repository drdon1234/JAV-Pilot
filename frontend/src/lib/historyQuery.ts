export function catalogHistoryQueryError(value: string): string {
  const normalized = value.normalize('NFKC').trim().toUpperCase()
  if (!normalized) return ''
  if (normalized.length > 40 || !/^[A-Z0-9._ -]+$/.test(normalized) || !/[A-Z0-9]/.test(normalized)) {
    return '仅支持 40 个以内的英文字母、数字、空格及 . _ -'
  }
  return ''
}
