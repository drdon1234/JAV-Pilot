// Revoking right after click() can cancel the save in some browsers, so the
// object URL stays alive long enough for the download to start.
const OBJECT_URL_LIFETIME_MS = 60_000

/** Hands a generated file to the browser's download flow. */
export function saveBlob(blob: Blob, filename: string): void {
  const objectUrl = URL.createObjectURL(blob)
  const link = document.createElement('a')
  link.href = objectUrl
  link.download = filename
  link.hidden = true
  document.body.append(link)
  link.click()
  link.remove()
  globalThis.setTimeout(() => URL.revokeObjectURL(objectUrl), OBJECT_URL_LIFETIME_MS)
}
