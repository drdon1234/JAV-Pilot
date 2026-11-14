import type { SiteCapability, WorkResult } from '../types'
import { t } from './i18n'

export const METADATA_PROFILES = ['javbus', 'javdb', 'fc2', 'fanza', 'mgs', 'avbase', 'fc2db'] as const
export const SEARCH_PARSER_PROFILES = new Set<string>([...METADATA_PROFILES, 'torznab'])
export const SEARCH_CAPABILITIES: SiteCapability[] = ['metadata_search', 'metadata_detail', 'torrent_search']
export const PROFILE_LABELS: Record<string, string> = {
  javbus: 'JavBus', javdb: 'JavDB', fc2: t('FC2（AVSOX + 官方 + PPV DataBank）'),
  fanza: 'FANZA', mgs: 'MGS', avbase: 'AVBase', fc2db: 'FC2DB',
  torznab: t('Torznab 种子索引'),
}

export function prioritizeMetadataWorks(results: readonly WorkResult[]): WorkResult[] {
  const priorities: Record<string, number> = { javbus: 0, javdb: 1, fc2: 2 }
  const priority = (work: WorkResult) => Math.min(3, ...work.sources.map((source) => (
    priorities[source.detail_provider || source.source_id] ?? 3
  )))
  return [...results].sort((left, right) => priority(left) - priority(right))
}

export function profileCapabilities(profile: string): SiteCapability[] {
  if (profile === 'torznab') return ['torrent_search']
  if (profile === 'fc2db') return ['metadata_detail']
  return ['metadata_search', 'metadata_detail']
}
