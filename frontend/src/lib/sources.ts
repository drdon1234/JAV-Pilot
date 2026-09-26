import type { SiteCapability } from '../types'

export const METADATA_PROFILES = ['javbus', 'javdb', 'fc2', 'fanza', 'mgs', 'avbase', 'fc2db', 'javten'] as const
export const SEARCH_PARSER_PROFILES = new Set<string>([...METADATA_PROFILES, 'torznab'])
export const SEARCH_CAPABILITIES: SiteCapability[] = ['metadata_search', 'metadata_detail', 'torrent_search']
export const PROFILE_LABELS: Record<string, string> = {
  javbus: 'JavBus', javdb: 'JavDB', fc2: 'FC2（AVSOX + 官方 + PPV DataBank）',
  fanza: 'FANZA', mgs: 'MGS', avbase: 'AVBase', fc2db: 'FC2DB', javten: 'JAVTEN',
  torznab: 'Torznab 种子索引',
}

export function profileCapabilities(profile: string): SiteCapability[] {
  if (profile === 'torznab') return ['torrent_search']
  if (profile === 'fc2db' || profile === 'javten') return ['metadata_detail']
  return ['metadata_search', 'metadata_detail']
}
