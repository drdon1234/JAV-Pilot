import { useEffect, useRef, useState } from 'react'
import { Captions, Check } from 'lucide-react'
import { createPortal } from 'react-dom'
import type { RefObject } from 'react'
import { PlayerMenu } from './PlayerMenu'
import { decodeSubtitleFile, parseSubtitles, type SubtitleCue, type SubtitleSource } from '../lib/playbackSubtitles'
import { requestJson } from '../lib/api'
import { t } from '../lib/i18n'

const defaults = { size: 24, offset: 0, bottom: 8, color: '#ffffff', background: true }
const noSources: SubtitleSource[] = []

export function PlayerSubtitles({ video, sources = noSources }: { video: RefObject<HTMLVideoElement | null>; sources?: SubtitleSource[] }) {
  const [settings, setSettings] = useState(() => {
    try {
      const saved = JSON.parse(localStorage.getItem('player-subtitle-style') || '{}')
      return { size: Math.max(16, Math.min(48, Number(saved.size) || defaults.size)), offset: 0, bottom: Math.max(3, Math.min(35, Number(saved.bottom) || defaults.bottom)), color: ['#ffffff', '#ffe066', '#86efac'].includes(saved.color) ? saved.color : defaults.color, background: saved.background !== false }
    } catch { return defaults }
  })
  const [selected, setSelected] = useState(sources[0]?.id ?? '')
  const [imported, setImported] = useState<{ id: string; label: string; cues: SubtitleCue[] } | null>(null)
  const [cues, setCues] = useState<SubtitleCue[]>([])
  const [text, setText] = useState('')
  const [status, setStatus] = useState('')
  const [attempt, setAttempt] = useState(0)
  const generation = useRef(0)
  const file = useRef<HTMLInputElement>(null)
  useEffect(() => {
    try { localStorage.setItem('player-subtitle-style', JSON.stringify({ ...settings, offset: 0 })) } catch { /* Storage may be disabled. */ }
  }, [settings])
  useEffect(() => {
    const current = ++generation.current
    const controller = new AbortController()
    setCues([]); setText(''); setStatus('')
    if (selected && selected === imported?.id) setCues(imported.cues)
    else {
      const source = sources.find(item => item.id === selected)
      if (source) {
        setStatus(t('正在加载字幕'))
        void requestJson<{ text: string }>(source.url, { signal: controller.signal }).then(result => {
          if (generation.current === current) { setCues(parseSubtitles(result.text)); setStatus('') }
        }).catch(() => { if (!controller.signal.aborted && generation.current === current) setStatus(t('字幕加载失败，可重试或导入字幕文件。')) })
      }
    }
    return () => { generation.current++; controller.abort() }
  }, [selected, sources, imported, attempt])
  useEffect(() => {
    const element = video.current!
    const update = () => {
      const time = element.currentTime - settings.offset
      // Binary search the upper bound, then allow overlapping dialogue.
      let left = 0, right = cues.length
      while (left < right) { const middle = (left + right) >>> 1; if (cues[middle].start <= time) left = middle + 1; else right = middle }
      const active = []
      for (let index = left - 1; index >= 0 && index >= left - 50; index--) if (cues[index].end > time) active.unshift(cues[index].text)
      setText(active.join('\n'))
    }
    for (const event of ['timeupdate', 'seeked', 'loadedmetadata']) element.addEventListener(event, update)
    update()
    return () => { for (const event of ['timeupdate', 'seeked', 'loadedmetadata']) element.removeEventListener(event, update) }
  }, [video, cues, settings.offset])
  const tracks = [...sources, ...(imported ? [imported] : [])]
  return <>
    {video.current?.parentElement ? createPortal(<div className="player-subtitle-overlay" style={{ bottom: `${settings.bottom}%`, fontSize: `clamp(16px, ${settings.size / 8}vw, ${settings.size}px)`, color: settings.color }} aria-live="off">
      {text ? <span className={settings.background ? 'subtitle-with-background' : ''}>{text}</span> : null}
    </div>, video.current.parentElement) : null}
    <div className="player-subtitle-control">
      <PlayerMenu label={t('字幕设置')} value={<Captions aria-hidden="true" />} wide>
        <div className="subtitle-settings">
          <div role="menu" aria-label={t('字幕轨道')}>
            {[{ id: '', label: t('关闭字幕') }, ...tracks].map(track => <button key={track.id} role="menuitemradio" aria-checked={selected === track.id} type="button" onClick={() => { setSelected(track.id); setSettings(previous => ({ ...previous, offset: 0 })) }}><span>{track.label}</span>{selected === track.id ? <Check aria-hidden="true" /> : null}</button>)}
          </div>
          {!sources.length ? <p>{t('未发现外挂字幕，可导入 SRT、VTT、ASS 或 SSA 文件。')}</p> : null}
          <input ref={file} className="sr-only" tabIndex={-1} aria-label={t('字幕文件')} type="file" accept=".srt,.vtt,.ass,.ssa" onChange={async event => {
            const selectedFile = event.target.files?.[0]
            event.target.value = ''
            if (!selectedFile) return
            const current = ++generation.current
            try {
              if (selectedFile.size > 4 * 1024 * 1024) throw new Error('Too large')
              const parsed = parseSubtitles(decodeSubtitleFile(await selectedFile.arrayBuffer()))
              if (current !== generation.current) return
              const id = `imported:${current}`
              setImported({ id, label: selectedFile.name, cues: parsed }); setSelected(id)
              setSettings(previous => ({ ...previous, offset: 0 }))
            } catch { if (current === generation.current) setStatus(t('字幕文件无法读取，请检查格式、编码和大小（最大 4 MB）。')) }
          }} />
          <button type="button" className="subtitle-import" onClick={() => file.current?.click()}>{t('导入字幕')}</button>
          {status ? <p role="status">{status}{status === t('字幕加载失败，可重试或导入字幕文件。') ? <button type="button" onClick={() => setAttempt(previous => previous + 1)}>{t('重试')}</button> : null}</p> : null}
          <label>{t('字幕延迟（秒）')}<input type="number" min={-600} max={600} step={0.5} value={settings.offset} onChange={event => setSettings(previous => ({ ...previous, offset: Math.max(-600, Math.min(600, Number(event.target.value) || 0)) }))} /></label>
          <p>{t('正数延后，负数提前；切换作品后归零。')}</p>
          <label>{t('字幕字号')}<input aria-label={t('字幕字号')} type="range" min={16} max={48} value={settings.size} onChange={event => setSettings(previous => ({ ...previous, size: Number(event.target.value) }))} /><output>{settings.size}</output></label>
          <label>{t('字幕位置')}<input aria-label={t('字幕位置')} type="range" min={3} max={35} value={settings.bottom} onChange={event => setSettings(previous => ({ ...previous, bottom: Number(event.target.value) }))} /><output>{settings.bottom}%</output></label>
          <fieldset><legend>{t('字幕颜色')}</legend>{[{ color: '#ffffff', label: t('白色') }, { color: '#ffe066', label: t('黄色') }, { color: '#86efac', label: t('绿色') }].map(option => <button type="button" key={option.color} aria-label={option.label} aria-pressed={settings.color === option.color} className="subtitle-color" style={{ color: option.color }} onClick={() => setSettings(previous => ({ ...previous, color: option.color }))}>{option.label}</button>)}</fieldset>
          <label><span>{t('字幕背景')}</span><input type="checkbox" checked={settings.background} onChange={event => setSettings(previous => ({ ...previous, background: event.target.checked }))} /></label>
          <button type="button" onClick={() => setSettings(defaults)}>{t('恢复默认')}</button>
        </div>
      </PlayerMenu>
    </div>
  </>
}
