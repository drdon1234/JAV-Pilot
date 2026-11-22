import { useQuery } from '@tanstack/react-query'
import { api } from './api'
import type { DownloadHistoryItem } from '../types'

// Share equivalent site codes and coalesce visible cards into bounded lookups.
export const codeKey = (code: string) => {
  const clean = code.normalize('NFKC').trim().toUpperCase()
  const fc2 = /^FC2(?:[-._\s]*PPV)?[-._\s]*(\d{2,9})$/.exec(clean)
  if (fc2) return `FC2PPV:${Number(fc2[1])}`
  const match = /^(.*[A-Z].*?)[-._\s]+(\d{1,9})$/.exec(clean) ?? /^([A-Z0-9]*?[A-Z])(\d{1,9})$/.exec(clean)
  return match ? `${match[1].replace(/[-._\s]/g, '')}:${Number(match[2])}` : clean
}
type Pending = { code: string; resolve: (item: DownloadHistoryItem | null) => void; reject: (error: unknown) => void }
let pending: Pending[] = []
let timer: ReturnType<typeof setTimeout> | undefined

export function lookupWorkAvailability(code: string): Promise<DownloadHistoryItem | null> {
  return new Promise((resolve, reject) => {
    pending.push({ code, resolve, reject })
    if (timer !== undefined) return
    timer = setTimeout(() => {
      const requests = pending
      pending = []
      timer = undefined
      for (let offset = 0; offset < requests.length; offset += 200) {
        const batch = requests.slice(offset, offset + 200)
        void api.downloadHistoryLookup(batch.map(item => item.code)).then(result => {
          if (result.unavailable.includes('library')) throw new Error('Local playback lookup unavailable')
          const items = new Map(result.items.map(item => [codeKey(item.code), item]))
          batch.forEach(item => item.resolve(items.get(codeKey(item.code)) ?? null))
        }).catch(error => batch.forEach(item => item.reject(error)))
      }
    }, 0)
  })
}

export function useLocalPlayback(code: string) {
  return useQuery({
    queryKey: ['local-playback-availability', codeKey(code)],
    queryFn: () => lookupWorkAvailability(code),
    enabled: Boolean(code.trim()) && code.length <= 64,
    staleTime: 15_000,
    refetchInterval: 30_000,
    retry: false,
  })
}
