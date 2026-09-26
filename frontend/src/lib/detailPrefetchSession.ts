import { api, ApiError } from './api'

const DETAIL_PREFETCH_BATCH_STORAGE_KEY = 'jav-pilot-detail-prefetch-batch'
const DETAIL_PREFETCH_BATCH_ID_PATTERN = /^[0-9a-f]{32}$/i

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
