export interface SubtitleSource { id: string; label: string; url: string }
export interface SubtitleCue { start: number; end: number; text: string }

function timestamp(value: string) {
  const parts = value.trim().replace(',', '.').split(':').map(Number)
  return parts.reduce((total, part) => total * 60 + part, 0)
}

export function parseSubtitles(document: string): SubtitleCue[] {
  const cues: SubtitleCue[] = []
  const text = document.replace(/^\uFEFF/, '').replace(/\r/g, '')
  const plain = (value: string) => value.replace(/\{[^}]*\}/g, '').replace(/<[^>]*>/g, '').replace(/\\[Nn]/g, '\n').replace(/\\h/g, ' ').replace(/&lt;/g, '<').replace(/&gt;/g, '>').replace(/&amp;/g, '&').replace(/&nbsp;/g, ' ').trim()
  const add = (start: string, end: string, value: string) => {
    const cue = { start: timestamp(start), end: timestamp(end), text: plain(value) }
    if (Number.isFinite(cue.start) && Number.isFinite(cue.end) && cue.end > cue.start && cue.text) cues.push(cue)
  }
  if (/^\[Events\]/mi.test(text)) {
    let fields = ['layer', 'start', 'end', 'style', 'name', 'marginl', 'marginr', 'marginv', 'effect', 'text']
    let events = false
    for (const line of text.split('\n')) {
      if (/^\[/.test(line)) events = /^\[Events\]/i.test(line)
      if (!events) continue
      if (/^Format:/i.test(line)) fields = line.slice(line.indexOf(':') + 1).split(',').map(field => field.trim().toLowerCase())
      if (!/^Dialogue:/i.test(line)) continue
      const values = line.slice(line.indexOf(':') + 1).trim().split(',')
      const index = fields.indexOf('text')
      if (index < 0) continue
      add(values[fields.indexOf('start')] ?? '', values[fields.indexOf('end')] ?? '', values.slice(index).join(','))
    }
  } else {
    const pattern = /(?:^|\n)((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})\s*-->\s*((?:\d+:)?\d{1,2}:\d{2}[.,]\d{1,3})[^\n]*\n([\s\S]*?)(?=\n\s*\n|$)/g
    for (const match of text.matchAll(pattern)) add(match[1], match[2], match[3])
  }
  if (!cues.length) throw new Error('No supported subtitle cues')
  return cues.sort((a, b) => a.start - b.start).slice(0, 100_000)
}

export function decodeSubtitleFile(buffer: ArrayBuffer): string {
  const bytes = new Uint8Array(buffer)
  for (const encoding of [bytes[0] === 0xff && bytes[1] === 0xfe ? 'utf-16le' : bytes[0] === 0xfe && bytes[1] === 0xff ? 'utf-16be' : 'utf-8', 'gb18030', 'big5', 'shift-jis']) {
    try { return new TextDecoder(encoding, { fatal: true }).decode(bytes) } catch { /* Try common sidecar encodings. */ }
  }
  throw new Error('Unsupported subtitle encoding')
}
