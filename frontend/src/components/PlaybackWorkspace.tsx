import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react'
import { Link, useLocation, useNavigate } from 'react-router-dom'
import { Play } from 'lucide-react'
import { SelectedWork } from '../pages/WatchPage'
import { t } from '../lib/i18n'
import { Button, PageHeader } from './ui'
import { LocalPlayer } from './LocalPlayer'
import { PlaybackActivity, PlaybackSession, type PlaybackSnapshot } from '../lib/playbackActivity'
import { PlaybackStart } from './PlaybackStart'
import { playbackTime } from './VideoControls'
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
  const [live, setLive] = useState(true)
  const [snapshot, setSnapshot] = useState<PlaybackSnapshot | null>(null)
  const autoplay = useRef(fresh)
  const capture = useCallback((next: PlaybackSnapshot) => setSnapshot(previous => ({ ...next, image: next.image || previous?.image || '' })), [])
  const session = useMemo(() => ({ autoplay, capture, snapshot }), [capture, snapshot])
  useLayoutEffect(() => {
    if (visible) return
    autoplay.current = false
    const timer = window.setTimeout(() => setLive(false), 30_000)
    return () => window.clearTimeout(timer)
  }, [visible])
  return <PlaybackSession.Provider value={session}><PlaybackActivity.Provider value={visible}>
    {live ? work.entry ? <LocalPlayer entry={work.entry} file={work.file!} /> : <SelectedWork source={work.source!} url={work.url!} play onPlay={() => undefined} /> : <section className="playback-frozen video-stage controls-visible" style={{ aspectRatio: snapshot?.aspect || 16 / 9 }} aria-label={t('应用内播放器')}>
      {snapshot?.image ? <img className="playback-frozen-frame" src={snapshot.image} alt={t('暂停画面')} /> : null}
      <button type="button" className="playback-frozen-play" aria-label={t('继续播放')} onClick={() => { autoplay.current = true; setLive(true) }}><Play aria-hidden="true" /></button>
      <div className="video-controls"><div className="video-progress-row"><span>{playbackTime(snapshot?.position || 0)}</span><progress aria-label={t('播放进度')} max={snapshot?.duration || 1} value={snapshot?.position || 0} /><span>{playbackTime(snapshot?.duration || 0)}</span></div><p>{t('已暂停，点击继续播放')}</p></div>
    </section>}
  </PlaybackActivity.Provider></PlaybackSession.Provider>
}
