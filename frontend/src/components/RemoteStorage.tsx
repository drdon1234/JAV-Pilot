import { useMutation, useQuery } from '@tanstack/react-query'
import { FileVideo, Folder } from 'lucide-react'
import { useRef, useState } from 'react'
import { useSearchParams } from 'react-router-dom'
import { requestJson } from '../lib/api'
import { t } from '../lib/i18n'
import { Button, InlineNotice, SkeletonRows } from './ui'

export function useStorageSources() {
  return useQuery({
    queryKey: ['storage-sources'],
    staleTime: 60_000,
    queryFn: () =>
      requestJson<{ mounts: string[]; cloud: boolean; fnos: boolean; direct: string[] }>(
        '/api/library/storage',
      ),
  })
}

export function StorageBrowser() {
  const [params, set] = useSearchParams()
  const mount = params.get('mount') || '',
    path = params.get('path') || '',
    offset = Number(params.get('offset') || 0)
  const selected = params.get('video') || ''
  const q = useStorageSources()
  const files = useQuery({
    queryKey: ['mounted-dir', mount, path, offset],
    enabled: Boolean(mount),
    retry: false,
    queryFn: () =>
      requestJson<{ items: { name: string; folder: boolean }[]; next: number | null }>(
        '/api/library/storage?' + new URLSearchParams({ mount, path, offset: String(offset) }),
      ),
  })
  function navigate(values: Record<string, string>) {
    set({ view: 'mounts', mount, path, ...values })
  }
  return (
    <div className="remote-storage">
      {q.isPending && <SkeletonRows />}
      <div className="watch-inline-actions">
        {q.data?.mounts.map((m) => (
          <Button key={m} aria-pressed={mount === m} onClick={() => navigate({ mount: m, path: '' })}>
            {m}
          </Button>
        ))}
      </div>
      {q.isError && <p role="alert">{q.error.message}</p>}
      {q.data && !q.data.mounts.length && <p>{t('尚未配置按需浏览挂载')}</p>}
      {mount && (
        <>
          <h2 className="storage-path">
            {mount} / {path}
          </h2>
          {path && (
            <Button onClick={() => navigate({ path: path.split('/').slice(0, -1).join('/') })}>
              {t('上级目录')}
            </Button>
          )}
          {files.isPending ? (
            <SkeletonRows />
          ) : files.isError ? (
            <p role="alert">{files.error.message}</p>
          ) : (
            <ul className="storage-entries">
              {files.data.items.map((f) => (
                <li key={f.name}>
                  <Button
                    onClick={() =>
                      navigate({ [f.folder ? 'path' : 'video']: [path, f.name].filter(Boolean).join('/') })
                    }
                  >
                    {f.folder ? <Folder aria-hidden="true" /> : <FileVideo aria-hidden="true" />}
                    {f.name}
                  </Button>
                </li>
              ))}
            </ul>
          )}
          {offset > 0 && (
            <Button onClick={() => navigate({ offset: String(Math.max(0, offset - 100)) })}>
              {t('上一页')}
            </Button>
          )}
          {files.data?.next != null && (
            <Button onClick={() => navigate({ offset: String(files.data?.next ?? 0) })}>{t('下一页')}</Button>
          )}
          {selected && q.data && (
            <MountedPlayer
              key={mount + '/' + selected}
              mount={mount}
              path={selected}
              direct={q.data.direct.includes(mount)}
            />
          )}
        </>
      )}
      {q.data?.fnos && <NasDownload />}
      {q.data?.cloud && <CloudTasks />}
    </div>
  )
}

export function MountedPlayer({ mount, path, direct }: { mount: string; path: string; direct: boolean }) {
  const [mode, setMode] = useState<'direct' | 'proxy'>(direct ? 'direct' : 'proxy')
  const [failed, setFailed] = useState(false)
  const position = useRef(0)
  const video = useRef<HTMLVideoElement>(null)
  const params = new URLSearchParams({ mount, path })
  const link = useQuery({
    queryKey: ['mounted-direct', mount, path],
    enabled: mode === 'direct' && direct,
    queryFn: () => requestJson<{ url: string }>('/api/library/storage?' + params + '&mode=direct'),
    retry: false,
    gcTime: 0,
    refetchOnWindowFocus: false,
    refetchOnReconnect: false,
  })
  const src = mode === 'direct' ? link.data?.url : '/api/library/mounted-media?' + params
  function change(next: 'direct' | 'proxy') {
    if (video.current?.currentTime) position.current = video.current.currentTime
    setFailed(false)
    setMode(next)
  }
  return (
    <section className="library-detail-files">
      <h3>{t('播放方式')}</h3>
      <div className="watch-inline-actions" role="group" aria-label={t('播放方式')}>
        <Button disabled={!direct} aria-pressed={mode === 'direct'} onClick={() => change('direct')}>
          {t('网盘直连播放')}
        </Button>
        <Button aria-pressed={mode === 'proxy'} onClick={() => change('proxy')}>
          {t('NAS 代理播放')}
        </Button>
      </div>
      <p>
        {mode === 'direct'
          ? t('视频由网盘直接发送到当前设备。')
          : t('NAS 从挂载读取视频，再发送到当前设备。')}
      </p>
      {!direct && <p>{t('此挂载尚未配置直链服务。')}</p>}
      {mode === 'direct' && link.isPending && <p role="status">{t('正在获取直链')}</p>}
      {(failed || (mode === 'direct' && link.isError)) && (
        <InlineNotice tone="warning">
          {t('播放失败')}
          {mode === 'direct' ? (
            <Button onClick={() => change('proxy')}>{t('切换 NAS 代理播放')}</Button>
          ) : (
            <Button
              onClick={() => {
                setFailed(false)
                video.current?.load()
              }}
            >
              {t('重试')}
            </Button>
          )}
        </InlineNotice>
      )}
      {src && (
        <video
          key={mode + src}
          ref={video}
          src={src}
          controls
          playsInline
          preload="metadata"
          style={{ width: '100%' }}
          onLoadedMetadata={(e) => {
            if (position.current && position.current < e.currentTarget.duration)
              e.currentTarget.currentTime = position.current
          }}
          onTimeUpdate={(e) => {
            if (e.currentTarget.currentTime > 0) position.current = e.currentTarget.currentTime
          }}
          onError={() => setFailed(true)}
        />
      )}
    </section>
  )
}

function NasDownload() {
  const add = useMutation({
    mutationFn: (magnet: string) =>
      requestJson<{ ids: number[] }>('/api/library/cloud-download', {
        method: 'POST',
        body: JSON.stringify({ provider: 'fnos', magnet }),
      }),
  })
  return (
    <section className="storage-download">
      <h2>{t('飞牛下载中心')}</h2>
      <form
        className="watch-inline-actions"
        onSubmit={(e) => {
          e.preventDefault()
          add.mutate(String(new FormData(e.currentTarget).get('magnet')))
        }}
      >
        <input name="magnet" required aria-label={t('磁链')} placeholder="magnet:?" />
        <Button type="submit" disabled={add.isPending}>
          {t('提交到 NAS')}
        </Button>
      </form>
      {add.isError && <p role="alert">{add.error.message}</p>}
      {add.data && <p role="status">{t('飞牛下载任务编号 {ids}', { ids: add.data.ids.join(', ') })}</p>}
    </section>
  )
}

function CloudTasks() {
  const [page, setPage] = useState(1)
  const q = useQuery({
    queryKey: ['cloud-tasks', page],
    queryFn: () =>
      requestJson<{
        tasks: { info_hash: string; name: string; percentDone: number; status: number }[]
        more: boolean
      }>('/api/library/storage?cloud=1&page=' + page),
  })
  const add = useMutation({
    mutationFn: (magnet: string) =>
      requestJson('/api/library/cloud-download', { method: 'POST', body: JSON.stringify({ magnet }) }),
    onSuccess: () => q.refetch(),
  })
  return (
    <section className="storage-download">
      <h2>{t('115 云下载')}</h2>
      <form
        className="watch-inline-actions"
        onSubmit={(e) => {
          e.preventDefault()
          add.mutate(String(new FormData(e.currentTarget).get('magnet')))
        }}
      >
        <input name="magnet" required aria-label={t('磁链')} placeholder="magnet:?" />
        <Button type="submit" disabled={add.isPending}>
          {t('提交到 115')}
        </Button>
      </form>
      {add.isError && <p role="alert">{add.error.message}</p>}
      {q.isError && <p role="alert">{q.error.message}</p>}
      <ul>
        {q.data?.tasks.map((row) => (
          <li key={row.info_hash}>
            {row.name} · {row.percentDone}%
          </li>
        ))}
      </ul>
      {page > 1 && <Button onClick={() => setPage(page - 1)}>{t('上一页')}</Button>}
      {q.data?.more && <Button onClick={() => setPage(page + 1)}>{t('下一页')}</Button>}
      <Button onClick={() => void q.refetch()}>{t('刷新')}</Button>
    </section>
  )
}
