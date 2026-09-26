export const DEFAULT_SEARCH_SNAPSHOT_LIMIT = 12
export const DEFAULT_SEARCH_SNAPSHOT_TTL_MS = 30 * 60 * 1_000

interface SnapshotEntry<T> {
  snapshot: T
  updatedAt: number
}

export interface SearchSnapshotCache<T> {
  readonly size: number
  remember: (key: string, snapshot: T) => void
  read: (key: string) => T | undefined
  remove: (key: string) => void
  clear: () => void
}

export function createSearchSnapshotCache<T>({
  maxEntries = DEFAULT_SEARCH_SNAPSHOT_LIMIT,
  ttlMs = DEFAULT_SEARCH_SNAPSHOT_TTL_MS,
  now = Date.now,
}: {
  maxEntries?: number
  ttlMs?: number
  now?: () => number
} = {}): SearchSnapshotCache<T> {
  const snapshots = new Map<string, SnapshotEntry<T>>()
  const entryLimit = Math.max(1, Math.floor(maxEntries))
  const lifetime = Math.max(1, ttlMs)

  function expired(entry: SnapshotEntry<T>): boolean {
    return now() - entry.updatedAt >= lifetime
  }

  function pruneExpired(): void {
    snapshots.forEach((entry, key) => {
      if (expired(entry)) snapshots.delete(key)
    })
  }

  return {
    get size() {
      pruneExpired()
      return snapshots.size
    },
    remember(key, snapshot) {
      if (!key) return
      pruneExpired()
      snapshots.delete(key)
      snapshots.set(key, { snapshot, updatedAt: now() })
      while (snapshots.size > entryLimit) {
        const oldest = snapshots.keys().next().value
        if (typeof oldest !== 'string') break
        snapshots.delete(oldest)
      }
    },
    read(key) {
      if (!key) return undefined
      const entry = snapshots.get(key)
      if (!entry) return undefined
      if (expired(entry)) {
        snapshots.delete(key)
        return undefined
      }
      snapshots.delete(key)
      snapshots.set(key, entry)
      return entry.snapshot
    },
    remove(key) {
      snapshots.delete(key)
    },
    clear() {
      snapshots.clear()
    },
  }
}
