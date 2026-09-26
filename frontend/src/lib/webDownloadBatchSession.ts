import { WEB_DOWNLOAD_VARIANTS } from './webDownloads'
import type {
  WebDownloadBatch,
  WebDownloadBatchItemIntent,
  WebDownloadVariant,
} from '../types'

export const WEB_DOWNLOAD_BATCH_SESSION_STORAGE_KEY = 'jav-pilot:web-download-batch-preview'
export const WEB_DOWNLOAD_BATCH_ROUTE_STATE_KEY = 'webDownloadBatchSession'

export interface WebDownloadBatchSession<TPreferences = unknown> {
  batch: WebDownloadBatch
  previewToken: string
  itemIntents?: WebDownloadBatchItemIntent[]
  intentsInitialized?: boolean
  preferences?: TPreferences
}

export function validWebDownloadBatchIdentity(value: unknown): value is string {
  return typeof value === 'string' && /^[A-Za-z0-9_-]{1,64}$/.test(value)
}

export function validWebDownloadBatchToken(value: unknown): value is string {
  return typeof value === 'string'
    && value.length > 0
    && value.length <= 256
    && /^[\x21-\x7e]+$/.test(value)
}

export function validWebDownloadVariantPriority(value: unknown): value is WebDownloadVariant[] {
  return Array.isArray(value)
    && value.length === WEB_DOWNLOAD_VARIANTS.length
    && WEB_DOWNLOAD_VARIANTS.every((variant) => value.includes(variant))
    && new Set(value).size === WEB_DOWNLOAD_VARIANTS.length
}

export function hasRecoverableWebDownloadBatchShape(value: unknown): value is WebDownloadBatch {
  if (!value || typeof value !== 'object') return false
  const batch = value as Partial<WebDownloadBatch>
  return validWebDownloadBatchIdentity(batch.batch_id)
    && typeof batch.status === 'string'
    && (batch.provenance_type === undefined
      || batch.provenance_type === 'series_discovery'
      || batch.provenance_type === 'resource_search_selection')
    && typeof batch.max_height === 'number'
    && validWebDownloadVariantPriority(batch.variant_priority)
    && Array.isArray(batch.items)
    && batch.items.every((item) => (
      Boolean(item)
      && typeof item.code === 'string'
      && WEB_DOWNLOAD_VARIANTS.includes(item.variant)
      && Array.isArray(item.available_heights)
    ))
}

export function isWebDownloadBatchIntent(value: unknown): value is WebDownloadBatchItemIntent {
  if (!value || typeof value !== 'object') return false
  const intent = value as Partial<WebDownloadBatchItemIntent>
  return typeof intent.code === 'string'
    && intent.code.length > 0
    && intent.code.length <= 80
    && WEB_DOWNLOAD_VARIANTS.includes(intent.variant as WebDownloadVariant)
    && (intent.quality_strategy === 'highest' || intent.quality_strategy === 'selected')
    && Number.isInteger(intent.requested_height)
    && Number(intent.requested_height) >= 144
    && Number(intent.requested_height) <= 4320
}

export function readWebDownloadBatchSession<TPreferences = unknown>(
  validatePreferences?: (value: unknown) => value is TPreferences,
): WebDownloadBatchSession<TPreferences> | null {
  if (typeof window === 'undefined') return null
  try {
    const raw = window.sessionStorage.getItem(WEB_DOWNLOAD_BATCH_SESSION_STORAGE_KEY)
    if (!raw) return null
    const value = validatedWebDownloadBatchSession<TPreferences>(JSON.parse(raw), validatePreferences)
    if (!value) {
      writeWebDownloadBatchSession(null)
      return null
    }
    return value
  } catch {
    writeWebDownloadBatchSession(null)
    return null
  }
}

export function readWebDownloadBatchRouteState(
  routeState: unknown,
): WebDownloadBatchSession | null {
  if (!routeState || typeof routeState !== 'object') return null
  return validatedWebDownloadBatchSession(
    (routeState as Record<string, unknown>)[WEB_DOWNLOAD_BATCH_ROUTE_STATE_KEY],
  )
}

function validatedWebDownloadBatchSession<TPreferences = unknown>(
  input: unknown,
  validatePreferences?: (value: unknown) => value is TPreferences,
): WebDownloadBatchSession<TPreferences> | null {
  if (!input || typeof input !== 'object') return null
  const value = input as Partial<WebDownloadBatchSession<TPreferences>>
  if (
    !hasRecoverableWebDownloadBatchShape(value.batch)
    || value.batch.provenance_type === 'resource_search_selection'
    || !validWebDownloadBatchToken(value.previewToken)
    || (value.itemIntents !== undefined && (
      !Array.isArray(value.itemIntents)
      || value.itemIntents.length > 999
      || !value.itemIntents.every(isWebDownloadBatchIntent)
    ))
    || (value.intentsInitialized !== undefined && typeof value.intentsInitialized !== 'boolean')
    || (validatePreferences && value.preferences !== undefined && !validatePreferences(value.preferences))
  ) return null
  return {
    batch: value.batch,
    previewToken: value.previewToken,
    ...(value.itemIntents ? { itemIntents: value.itemIntents } : {}),
    ...(typeof value.intentsInitialized === 'boolean' ? { intentsInitialized: value.intentsInitialized } : {}),
    ...(value.preferences !== undefined ? { preferences: value.preferences as TPreferences } : {}),
  }
}

export function writeWebDownloadBatchSession<TPreferences = unknown>(
  value: WebDownloadBatchSession<TPreferences> | null,
): boolean {
  if (typeof window === 'undefined') return false
  try {
    if (!value) {
      window.sessionStorage.removeItem(WEB_DOWNLOAD_BATCH_SESSION_STORAGE_KEY)
      return true
    }
    if (value.batch.provenance_type === 'resource_search_selection') {
      window.sessionStorage.removeItem(WEB_DOWNLOAD_BATCH_SESSION_STORAGE_KEY)
      return true
    }
    window.sessionStorage.setItem(WEB_DOWNLOAD_BATCH_SESSION_STORAGE_KEY, JSON.stringify(value))
    return true
  } catch {
    return false
  }
}
