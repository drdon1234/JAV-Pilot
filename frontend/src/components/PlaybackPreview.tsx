import { ArrowLeft, Play } from 'lucide-react'
import { usePlaybackActivity } from '../lib/playbackActivity'
import { t } from '../lib/i18n'
import { playbackTime } from '../lib/format'

export function PlaybackPreview({ onResume }: { onResume?: () => void }) {
  const { snapshot, exit } = usePlaybackActivity()
  return <section className="playback-frozen video-stage controls-visible" style={{ aspectRatio: snapshot?.aspect || 16 / 9 }} aria-label={t('应用内播放器')}>
    {snapshot?.image ? <img className="playback-frozen-frame" src={snapshot.image} alt={t('暂停画面')} /> : null}
    {exit ? <button type="button" className="video-exit" aria-label={t('返回')} onClick={exit}><ArrowLeft aria-hidden="true" /></button> : null}
    {onResume ? <button type="button" className="playback-frozen-play" aria-label={t('继续播放')} onClick={onResume}><Play aria-hidden="true" /></button> : <div className="video-buffering" role="status" aria-label={t('正在加载')}><span className="video-buffering-spinner" /></div>}
    <div className="video-controls"><div className="video-progress-row"><span>{playbackTime(snapshot?.position || 0)}</span><progress aria-label={t('播放进度')} max={snapshot?.duration || 1} value={snapshot?.position || 0} /><span>{playbackTime(snapshot?.duration || 0)}</span></div><p>{onResume ? t('已暂停，点击继续播放') : t('正在加载')}</p></div>
  </section>
}
