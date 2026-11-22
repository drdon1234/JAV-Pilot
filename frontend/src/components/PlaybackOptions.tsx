import { useState } from 'react'
import { useMutation } from '@tanstack/react-query'
import { requestJson } from '../lib/api'
import { t } from '../lib/i18n'
import { Button } from './ui'

export function PlaybackOptions({ entry, file }: { entry: string; file: number }) {
  const [compatible, setCompatible] = useState(false)
  const grant = useMutation({
    mutationFn: async () => {
      const data = await requestJson<{ url: string }>('/api/library/external-grant', {
        method: 'POST',
        body: JSON.stringify({ entry, file }),
      })
      return new URL(data.url, location.origin).href
    },
  })
  return (
    <details>
      <summary>{t('更多播放方式')}</summary>
      <div className="watch-inline-actions">
        <Button disabled={grant.isPending} onClick={() => grant.mutate()}>
          {t('生成限时播放链接')}
        </Button>
        <Button aria-pressed={compatible} onClick={() => setCompatible(!compatible)}>
          {compatible ? t('关闭兼容播放') : t('兼容播放')}
        </Button>
      </div>
      {grant.isError && <p role="alert">{grant.error.message}</p>}
      {grant.data && (
        <div className="watch-inline-actions">
          <input
            readOnly
            aria-label={t('限时播放链接')}
            value={grant.data}
            onFocus={(e) => e.target.select()}
          />
          <a
            download="video.m3u"
            href={'data:audio/x-mpegurl;charset=utf-8,' + encodeURIComponent('#EXTM3U\n' + grant.data + '\n')}
          >
            {t('下载 M3U')}
          </a>
        </div>
      )}
      {compatible && (
        <video
          autoPlay
          controls
          playsInline
          style={{ width: '100%' }}
          src={'/api/library/compatible-media?' + new URLSearchParams({ entry, file: String(file) })}
        />
      )}
    </details>
  )
}
