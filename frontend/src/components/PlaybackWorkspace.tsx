import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { SelectedWork } from '../pages/WatchPage'
import { t } from '../lib/i18n'
import { Button, PageHeader } from './ui'
import { LocalPlayer } from './LocalPlayer'
import { PlaybackActivity, PlaybackSession, type PlaybackSnapshot } from '../lib/playbackActivity'
import { PlaybackStart } from './PlaybackStart'
import { PlaybackPreview } from './PlaybackPreview'
import { BackLink } from './BackLink'

type PlayingWork = { source: string; url: string; entry?: never; file?: never } | { entry: string; file: number; source?: never; url?: never }
const workKey = (work: PlayingWork | null) => work ? work.entry ? `local:${work.entry}:${work.file}` : `online:${work.source}:${work.url}` : ''

export function PlaybackWorkspace() {
  const location = useLocation()
  const navigate = useNavigate()
  const [work, setWork] = useState<PlayingWork | null>(() => {
    try { return JSON.parse(sessionStorage.getItem('watch-current') || 'null') } catch { return null }
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
        {current ? <PlaybackSurface key={workKey(current)} work={current} visible={visible} fresh={!!requested && workKey(requested) !== workKey(work)} /> : visible ? <PlaybackStart /> : null}
      </div>
    </div>
  </>
}

function PlaybackSurface({ work, visible, fresh }: { work: PlayingWork; visible: boolean; fresh: boolean }) {
  const navigate = useNavigate()
  const exit = useCallback(() => navigate(work.entry ? '/library' : '/watch'), [navigate, work.entry])
  const [live, setLive] = useState(true)
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
