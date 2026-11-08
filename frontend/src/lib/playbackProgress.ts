import { requestJson } from './api'

// Complete the previous player's last save before reading or writing a new
// session. This also handles immediately reopening a work after navigation.
let pending: Promise<unknown> = Promise.resolve()

export function waitForPlaybackSaves() { return pending }

export function savePlaybackProgress(url: string, body: string) {
  const request = pending.then(() => requestJson(url, { method: 'POST', body, keepalive: true }))
  pending = request.catch(() => undefined)
  return request
}
