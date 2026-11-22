import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useRef, useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { requestJson } from '../lib/api'
import { browserImportScript } from '../lib/browserImport'
import { saveBlob } from '../lib/saveBlob'
import { t } from '../lib/i18n'
import { useToast } from './ToastProvider'
import { Button, PageHeader, InlineNotice, SkeletonRows } from './ui'
import { QuickWebDownload } from '../pages/QuickWebDownload'

type Entry = { entry_id: string; code: string | null; title: string | null }
type Explore = {
  entries: Entry[]
  has_more: boolean
  assets: number
  works: number
  playlists: string[]
  locations: { folder: string; total: number }[]
}
const playlistApi = (name: string, entry: string, action: string) =>
  requestJson('/api/library/playlist', { method: 'POST', body: JSON.stringify({ name, entry, action }) })

export function PlaylistEditor({ entry }: { entry: string }) {
  const [name, setName] = useState('')
  const toast = useToast()
  const client = useQueryClient()
  const save = useMutation({
    mutationFn: (list: string) => playlistApi(list, entry, 'add'),
    onSuccess: () => {
      void client.invalidateQueries({ queryKey: ['explore'] })
      toast.push(t('已加入播放列表'), 'success')
    },
    onError: (e: Error) => toast.push(e.message, 'error'),
  })
  return (
    <details>
      <summary>{t('加入播放列表')}</summary>
      <form
        className="watch-inline-actions"
        onSubmit={(e) => {
          e.preventDefault()
          save.mutate(name)
        }}
      >
        <Button disabled={save.isPending} type="button" onClick={() => save.mutate('稍后看')}>
          {t('稍后看')}
        </Button>
        <input
          required
          maxLength={80}
          aria-label={t('列表名称')}
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <Button disabled={save.isPending} type="submit">
          {t('加入列表')}
        </Button>
      </form>
    </details>
  )
}

export function ExplorePage() {
  const [params, set] = useSearchParams()
  const view = params.get('view') || 'unseen',
    name = params.get('name') || '',
    offset = Number(params.get('offset') || 0)
  const intake = params.get('code')
  const q = useQuery({
    queryKey: ['explore', view, name, offset],
    queryFn: () =>
      requestJson<Explore>(
        '/api/library/explore?' + new URLSearchParams({ view, name, offset: String(offset) }),
      ),
    enabled: !intake && view !== 'mounts' && view !== 'settings',
  })
  const remove = useMutation({
    mutationFn: (entry: string) => playlistApi(name, entry, 'remove'),
    onSuccess: () => q.refetch(),
  })
  const choices = {
    unseen: t('未看作品'),
    recommended: t('根据观看记录推荐'),
    playlist: t('播放列表'),
    storage: t('馆藏目录'),
    mounts: t('远程挂载与云下载'),
    settings: t('资料与浏览器接入'),
  }
  if (intake)
    return (
      <div className="page">
        <PageHeader title={intake} />
        <div data-browser-import-code={intake}>
          <QuickWebDownload code={intake} />
        </div>
      </div>
    )
  return (
    <div className="page">
      <PageHeader
        title={t('馆藏探索')}
        description={
          q.data
            ? t('{works} 部已识别作品 · {assets} 个馆藏条目', { works: q.data.works, assets: q.data.assets })
            : undefined
        }
      />
      <nav className="watch-source-tabs">
        {Object.entries(choices).map(([key, label]) => (
          <Button key={key} aria-pressed={view === key} onClick={() => set({ view: key })}>
            {label}
          </Button>
        ))}
      </nav>
      {view === 'settings' ? (
        <SourceSettings />
      ) : view === 'mounts' ? (
        <StorageBrowser />
      ) : q.isPending ? (
        <SkeletonRows />
      ) : q.isError ? (
        <InlineNotice tone="danger">{q.error.message}</InlineNotice>
      ) : (
        <>
          {view === 'playlist' && (
            <nav className="watch-inline-actions">
              {q.data.playlists.map((list) => (
                <Button key={list} aria-pressed={name === list} onClick={() => set({ view, name: list })}>
                  {list === '稍后看' ? t('稍后看') : list}
                </Button>
              ))}
            </nav>
          )}
          {view === 'storage' && (
            <nav className="watch-inline-actions">
              {q.data.locations.map((row) => (
                <Button
                  key={row.folder}
                  aria-pressed={name === row.folder}
                  onClick={() => set({ view, name: row.folder })}
                >
                  {row.folder} ({row.total})
                </Button>
              ))}
            </nav>
          )}
          <ul>
            {q.data.entries.map((entry) => (
              <li key={entry.entry_id}>
                <Link to={'/library?entry=' + entry.entry_id}>
                  {entry.code} {entry.title || t('本地媒体')}
                </Link>{' '}
                <Link
                  to={
                    '/player?' +
                    new URLSearchParams({
                      entry: entry.entry_id,
                      ...(view === 'playlist' ? { playlist: name } : {}),
                    })
                  }
                >
                  {t('播放')}
                </Link>
                {view === 'playlist' && (
                  <Button
                    size="small"
                    disabled={remove.isPending}
                    onClick={() => remove.mutate(entry.entry_id)}
                  >
                    {t('移出列表')}
                  </Button>
                )}
              </li>
            ))}
          </ul>
          {!q.data.entries.length && (
            <p>
              {view === 'recommended'
                ? t('有观看记录并匹配演员或标签后会显示推荐。')
                : t('暂无符合条件的馆藏作品')}
            </p>
          )}
          {remove.isError && <InlineNotice tone="danger">{remove.error.message}</InlineNotice>}
          <div className="watch-inline-actions">
            {offset > 0 && (
              <Button onClick={() => set({ view, name, offset: String(Math.max(0, offset - 50)) })}>
                {t('上一页')}
              </Button>
            )}
            {q.data.has_more && (
              <Button onClick={() => set({ view, name, offset: String(offset + 50) })}>{t('下一页')}</Button>
            )}
          </div>
        </>
      )}
    </div>
  )
}

function SourceSettings() {
  const q = useQuery({
    queryKey: ['custom-source'],
    queryFn: () =>
      requestJson<{ url: string; fields: Record<string, string> }>('/api/metadata/custom-source'),
  })
  const toast = useToast()
  const save = useMutation({
    mutationFn: (value: { url: string; fields: string }) =>
      requestJson('/api/metadata/custom-source', {
        method: 'POST',
        body: JSON.stringify({ url: value.url, fields: JSON.parse(value.fields) }),
      }),
    onSuccess: () => {
      void q.refetch()
      toast.push(t('已保存'), 'success')
    },
    onError: (e: Error) => toast.push(e.message, 'error'),
  })
  function install() {
    const contents = browserImportScript(location.origin)
    saveBlob(new Blob([contents], { type: 'text/javascript' }), 'jav-pilot.user.js')
  }
  return (
    <section>
      <h2>{t('浏览器入库')}</h2>
      <Button onClick={install}>{t('下载用户脚本')}</Button>
      <h2>{t('自定义资料源')}</h2>
      {q.isError ? (
        <p role="alert">{q.error.message}</p>
      ) : (
        q.data && (
          <form
            className="settings-form"
            onSubmit={(e) => {
              e.preventDefault()
              const d = new FormData(e.currentTarget)
              save.mutate({ url: String(d.get('url')), fields: String(d.get('fields')) })
            }}
          >
            <label>
              {t('公开 HTTPS 接口，番号用 {code} 表示')}
              <input name="url" defaultValue={q.data.url} />
            </label>
            <label>
              {t('字段与 JSON 路径')}
              <textarea name="fields" rows={5} defaultValue={JSON.stringify(q.data.fields, null, 2)} />
            </label>
            <Button type="submit" disabled={save.isPending}>
              {t('保存')}
            </Button>
          </form>
        )
      )}
    </section>
  )
}

function StorageBrowser() {
  const [params, set] = useSearchParams()
  const mount = params.get('mount') || '',
    path = params.get('path') || '',
    offset = Number(params.get('offset') || 0)
  const selected = params.get('video') || ''
  const q = useQuery({
    queryKey: ['storage-sources'],
    queryFn: () =>
      requestJson<{ mounts: string[]; cloud: boolean; fnos: boolean; direct: string[] }>(
        '/api/library/storage',
      ),
  })
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
    <section>
      <nav className="watch-inline-actions">
        {q.data?.mounts.map((m) => (
          <Button key={m} aria-pressed={mount === m} onClick={() => navigate({ mount: m, path: '' })}>
            {m}
          </Button>
        ))}
      </nav>
      {q.isError && <p role="alert">{q.error.message}</p>}
      {q.data && !q.data.mounts.length && <p>{t('尚未配置按需浏览挂载')}</p>}
      {mount && (
        <>
          <h2>
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
            <ul>
              {files.data.items.map((f) => (
                <li key={f.name}>
                  <Button
                    onClick={() =>
                      navigate({ [f.folder ? 'path' : 'video']: [path, f.name].filter(Boolean).join('/') })
                    }
                  >
                    {f.name}
                    {f.folder ? '/' : ''}
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
    </section>
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
    <section>
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
    <section>
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
