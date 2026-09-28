import { api, ApiError } from './api'
import type { DetailPrefetchBatch } from '../types'
import { t } from './i18n'

const DETAIL_PREFETCH_BATCH_STORAGE_KEY = 'jav-pilot-detail-prefetch-batch'
const DETAIL_PREFETCH_BATCH_ID_PATTERN = /^[0-9a-f]{32}$/i

export function detailPrefetchIsActive(batch: DetailPrefetchBatch | undefined): boolean {
  return batch?.status === 'queued' || batch?.status === 'running'
}

export function detailPrefetchStatusLabel(batch: DetailPrefetchBatch, cancelling = false): string {
  if (cancelling) return t('正在取消后台解析')
  if (detailPrefetchIsActive(batch)) return t('后台解析中')
  if (batch.status === 'failed') return t('后台解析失败')
  if (batch.status === 'partial') return t('后台解析部分完成')
  return t('后台解析完成')
}

export function storedDetailPrefetchBatchId(): string {
  try {
    return window.localStorage.getItem(DETAIL_PREFETCH_BATCH_STORAGE_KEY)?.trim() ?? ''
  } catch {
    return ''
  }
}

export function persistDetailPrefetchBatchId(batchId: string): void {
  try {
    window.localStorage.setItem(DETAIL_PREFETCH_BATCH_STORAGE_KEY, batchId)
  } catch {
    // The backend task still continues when persistent browser storage is unavailable.
  }
}

export function clearPersistedDetailPrefetchBatchId(expectedBatchId?: string): void {
  try {
    if (
      expectedBatchId
      && window.localStorage.getItem(DETAIL_PREFETCH_BATCH_STORAGE_KEY)?.trim() !== expectedBatchId
    ) return
    window.localStorage.removeItem(DETAIL_PREFETCH_BATCH_STORAGE_KEY)
  } catch {
    // Storage cleanup is best-effort; server cancellation remains authoritative.
  }
}

export async function clearAuthenticatedDetailPrefetchBatch(): Promise<void> {
  const batchId = storedDetailPrefetchBatchId()
  if (!batchId) return
  if (!DETAIL_PREFETCH_BATCH_ID_PATTERN.test(batchId)) {
    clearPersistedDetailPrefetchBatchId(batchId)
    return
  }
  try {
    await api.cancelDetailPrefetchBatch(batchId)
  } catch (error) {
    if (!(error instanceof ApiError && error.status === 404)) throw error
  }
  clearPersistedDetailPrefetchBatchId(batchId)
}
