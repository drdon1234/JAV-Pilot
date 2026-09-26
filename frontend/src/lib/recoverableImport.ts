type RouteManifestEntry = {
  css?: unknown
  file?: unknown
  name?: unknown
}

type RouteManifest = Record<string, RouteManifestEntry>

let routeManifestPromise: Promise<RouteManifest> | null = null
const recoveredStylesheets = new Map<string, Promise<void>>()
const routeScriptPattern = /^assets\/[A-Za-z0-9._-]+\.js$/
const routeStylesheetPattern = /^assets\/[A-Za-z0-9._-]+\.css$/

function manifestEntry(
  manifest: RouteManifest,
  manifestKey: string,
  chunkName: string,
): RouteManifestEntry | undefined {
  const direct = manifest[manifestKey]
  if (direct) return direct
  const named = Object.values(manifest).filter((entry) => entry.name === chunkName)
  return named.length === 1 ? named[0] : undefined
}

function recoveryAssetUrl(assetPath: string, recoveryToken: string): URL {
  const assetUrl = new URL(`/${assetPath}`, window.location.origin)
  if (assetUrl.origin !== window.location.origin) {
    throw new Error('route asset is unavailable')
  }
  assetUrl.searchParams.set('_recover', recoveryToken.slice(0, 64))
  return assetUrl
}

function existingStylesheet(assetUrl: URL): HTMLLinkElement | undefined {
  return Array.from(document.querySelectorAll<HTMLLinkElement>('link[rel="stylesheet"][href]')).find((link) => {
    try {
      const current = new URL(link.href, window.location.href)
      return current.origin === assetUrl.origin && current.pathname === assetUrl.pathname
    } catch {
      return false
    }
  })
}

function loadRecoveredStylesheet(assetPath: string, recoveryToken: string): Promise<void> {
  const assetUrl = recoveryAssetUrl(assetPath, recoveryToken)
  const cacheKey = `${assetUrl.origin}${assetUrl.pathname}`
  const cached = recoveredStylesheets.get(cacheKey)
  if (cached) return cached
  if (existingStylesheet(assetUrl)) {
    const loaded = Promise.resolve()
    recoveredStylesheets.set(cacheKey, loaded)
    return loaded
  }

  const link = document.createElement('link')
  link.rel = 'stylesheet'
  link.href = assetUrl.href
  link.dataset.routeRecoveryStylesheet = 'true'
  const loading = new Promise<void>((resolve, reject) => {
    const cleanup = () => {
      link.removeEventListener('load', handleLoad)
      link.removeEventListener('error', handleError)
    }
    const handleLoad = () => {
      cleanup()
      resolve()
    }
    const handleError = () => {
      cleanup()
      link.remove()
      recoveredStylesheets.delete(cacheKey)
      reject(new Error('route stylesheet is unavailable'))
    }
    link.addEventListener('load', handleLoad)
    link.addEventListener('error', handleError)
  })
  recoveredStylesheets.set(cacheKey, loading)
  document.head.append(link)
  return loading
}

export async function loadRouteStylesheetsForRecovery(
  entry: RouteManifestEntry,
  recoveryToken: string,
): Promise<void> {
  if (entry.css === undefined) return
  if (
    !Array.isArray(entry.css)
    || entry.css.some((assetPath) => typeof assetPath !== 'string' || !routeStylesheetPattern.test(assetPath))
  ) {
    throw new Error('route stylesheet manifest is invalid')
  }
  const stylesheets = Array.from(new Set(entry.css as string[]))
  await Promise.all(stylesheets.map((assetPath) => loadRecoveredStylesheet(assetPath, recoveryToken)))
}

export async function importRouteEntryForRecovery<T extends object>(
  entry: RouteManifestEntry,
  recoveryToken: string,
  importModule: (assetUrl: string) => Promise<T>,
): Promise<T> {
  const resolvedFile = entry.file
  if (typeof resolvedFile !== 'string' || !routeScriptPattern.test(resolvedFile)) {
    throw new Error('route chunk is unavailable')
  }
  await loadRouteStylesheetsForRecovery(entry, recoveryToken)
  return importModule(recoveryAssetUrl(resolvedFile, recoveryToken).href)
}

export async function recoverableImport<T extends object>(
  manifestKey: string,
  chunkName: string,
  load: () => Promise<T>,
): Promise<T> {
  const recoveryToken = new URLSearchParams(window.location.search).get('_recover')
  if (!import.meta.env.PROD || !recoveryToken) return load()

  routeManifestPromise ??= fetch('/assets/manifest.json', {
    cache: 'no-store',
    credentials: 'same-origin',
    headers: { Accept: 'application/json' },
  }).then(async (response) => {
    if (!response.ok) throw new Error('route manifest is unavailable')
    const payload: unknown = await response.json()
    if (!payload || typeof payload !== 'object' || Array.isArray(payload)) {
      throw new Error('route manifest is invalid')
    }
    return payload as RouteManifest
  })

  const manifest = await routeManifestPromise
  const entry = manifestEntry(manifest, manifestKey, chunkName)
  if (!entry) throw new Error('route chunk is unavailable')
  return importRouteEntryForRecovery(
    entry,
    recoveryToken,
    (assetUrl) => import(/* @vite-ignore */ assetUrl) as Promise<T>,
  )
}
