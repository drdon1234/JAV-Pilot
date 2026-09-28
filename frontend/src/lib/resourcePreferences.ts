export interface ResourceFormPreferences {
  resultLimit: number
  exactMatch: boolean
}

const STORAGE_KEY = 'jav-pilot:resource-search-form'

export const BUILTIN_RESOURCE_PREFERENCES: ResourceFormPreferences = {
  resultLimit: 100,
  exactMatch: false,
}

function clean(value: unknown, fallback: ResourceFormPreferences): ResourceFormPreferences {
  const input = value && typeof value === 'object' ? value as Record<string, unknown> : {}
  const resultLimit = Number(input.resultLimit)
  return {
    resultLimit: Number.isInteger(resultLimit) && resultLimit >= 1 && resultLimit <= 999 ? resultLimit : fallback.resultLimit,
    exactMatch: typeof input.exactMatch === 'boolean' ? input.exactMatch : fallback.exactMatch,
  }
}

/** The last Web resource search options used in this browser. */
export function loadResourcePreferences(
  defaults: ResourceFormPreferences = BUILTIN_RESOURCE_PREFERENCES,
): ResourceFormPreferences {
  try {
    const raw = globalThis.localStorage?.getItem(STORAGE_KEY)
    return raw ? clean(JSON.parse(raw), defaults) : defaults
  } catch {
    return defaults
  }
}

export function saveResourcePreferences(update: Partial<ResourceFormPreferences>): void {
  try {
    const current = loadResourcePreferences()
    globalThis.localStorage?.setItem(STORAGE_KEY, JSON.stringify(clean({ ...current, ...update }, current)))
  } catch {
    // Remembering options is a convenience; the form still works without storage.
  }
}
