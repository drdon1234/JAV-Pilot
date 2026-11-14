import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { recoverableImport } from '../lib/recoverableImport'
import { t } from '../lib/i18n'
import { Button, PageHeader } from './ui'
import { PlaybackActivity, PlaybackSession, type PlaybackSnapshot } from '../lib/playbackActivity'
import { PlaybackPreview } from './PlaybackPreview'
import { BackLink } from './BackLink'

const SelectedWork = lazy(() => recoverableImport('src/pages/WatchPage.tsx', 'WatchPage', () => import('../pages/WatchPage')).then(module => ({ default: module.SelectedWork })))
const LocalPlayer = lazy(() => recoverableImport('src/components/LocalPlayer.tsx', 'LocalPlayer', () => import('./LocalPlayer')).then(module => ({ default: module.LocalPlayer })))
const PlaybackStart = lazy(() => recoverableImport('src/components/PlaybackStart.tsx', 'PlaybackStart', () => import('./PlaybackStart')).then(module => ({ default: module.PlaybackStart })))

type PlayingWork = { source: string; url: string; entry?: never; file?: never } | { entry: string; file: number; source?: never; url?: never }
const workKey = (work: PlayingWork | null) => work ? work.entry ? `local:${work.entry}:${work.file}` : `online:${work.source}:${work.url}` : ''

export function readPlayingWork(value: unknown): PlayingWork | null {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return null
  const saved = value as Record<string, unknown>
  if (typeof saved.entry === 'string' && saved.entry.length > 0 && saved.entry.length <= 256
    && Number.isSafeInteger(saved.file) && (saved.file as number) >= 0) {
    return { entry: saved.entry, file: saved.file as number }
  }
  if (typeof saved.source === 'string' && /^[a-z0-9_-]{1,64}$/.test(saved.source)
    && typeof saved.url === 'string' && saved.url.length > 0 && saved.url.length <= 4096) {
    return { source: saved.source, url: saved.url }
  }
  return null
}

export function PlaybackWorkspace() {
  const location = useLocation()
  const navigate = useNavigate()
  const [work, setWork] = useState<PlayingWork | null>(() => {
    try { return readPlayingWork(JSON.parse(sessionStorage.getItem('watch-current') || 'null')) } catch { return null }
  })
  const visible = location.pathname === '/player'
  const requested = useMemo<PlayingWork | null>(() => {
    if (!visible) return null
    const params = new URLSearchParams(location.search)
    const entry = params.get('entry'), source = params.get('source'), url = params.get('url')
    const file = Number(params.get('file') || 0)
    return entry ? { entry, file: Number.isSafeInteger(file) && file >= 0 ? file : 0 } : source && url ? { source, url } : null
  }, [visible, location.search])
  const current = requested ?? work
  useEffect(() => {
    if (!requested) return
    setWork(previous => workKey(previous) === workKey(requested) ? previous : requested)
    try { sessionStorage.setItem('watch-current', JSON.stringify(requested)) } catch { /* Storage may be disabled. */ }
  }, [requested])
  return <>
    {current && !visible ? <Link className="button button-secondary playback-return" to="/player">{t('返回播放器')}</Link> : null}
    <div hidden={!visible}>
      <div className="page watch-page">
        {current ? <BackLink fallback={current.entry ? '/library' : '/watch'} /> : null}
        <PageHeader title={t('播放器')} actions={current ? <>{current.entry ? <Link className="button button-ghost" to={`/library?entry=${encodeURIComponent(current.entry)}`}>{t('作品详情')}</Link> : null}<Button type="button" onClick={() => {
          setWork(null)
          try { sessionStorage.removeItem('watch-current') } catch { /* Storage may be disabled. */ }
          navigate('/player', { replace: true })
        }}>{t('关闭播放')}</Button></> : undefined} />
        <Suspense fallback={<p role="status">{t('正在加载')}</p>}>
          {current ? <PlaybackSurface key={workKey(current)} work={current} visible={visible} fresh={!!requested && workKey(requested) !== workKey(work)} /> : visible ? <PlaybackStart /> : null}
        </Suspense>
      </div>
    </div>
  </>
}

function PlaybackSurface({ work, visible, fresh }: { work: PlayingWork; visible: boolean; fresh: boolean }) {
  const navigate = useNavigate()
  const exit = useCallback(() => navigate(work.entry ? '/library' : '/watch'), [navigate, work.entry])
  const [live, setLive] = useState(visible)
  const [snapshot, setSnapshot] = useState<PlaybackSnapshot | null>(null)
  const autoplay = useRef(fresh)
  const capture = useCallback((next: PlaybackSnapshot) => setSnapshot(previous => ({ ...next, image: next.image || previous?.image || '' })), [])
  const session = useMemo(() => ({ autoplay, capture, snapshot, exit }), [capture, snapshot, exit])
  useLayoutEffect(() => {
    if (visible) return
    autoplay.current = false
    const timer = window.setTimeout(() => setLive(false), 30_000)
    return () => window.clearTimeout(timer)
  }, [visible])
  return <PlaybackSession.Provider value={session}><PlaybackActivity.Provider value={visible}>
    {live ? work.entry ? <LocalPlayer entry={work.entry} file={work.file!} /> : <SelectedWork source={work.source!} url={work.url!} play onPlay={() => undefined} /> : <PlaybackPreview onResume={() => { autoplay.current = true; setLive(true) }} />}
  </PlaybackActivity.Provider></PlaybackSession.Provider>
}
