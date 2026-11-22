import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query'
import { useState } from 'react'
import { Link, useSearchParams } from 'react-router-dom'
import { api, requestJson } from '../lib/api'
import { codeKey as keyOf } from '../lib/localPlayback'
import { t } from '../lib/i18n'
import { webDownloadVariantLabel } from '../lib/webDownloads'
import { useToast } from './ToastProvider'
import { Button, EmptyState, InlineNotice, PageHeader, SkeletonRows } from './ui'

type Kind = 'want' | 'person'
interface Tracked {
  id: string
  kind: Kind
  label: string
  aliases: string[]
  checked: number | null
  height: number
  variant: string
  status: 'pending' | 'found' | 'empty' | 'error'
  results: { code: string; work_id: string; title: string }[]
}

function useTracking() {
  return useQuery({
    queryKey: ['tracking'],
    queryFn: () => requestJson<{ items: Tracked[] }>('/api/tracking'),
    staleTime: 15000,
  })
}

function useChange() {
  const client = useQueryClient()
  const toast = useToast()
  return useMutation({
    mutationFn: (body: Record<string, unknown>) =>
      requestJson('/api/tracking/action', { method: 'POST', body: JSON.stringify(body) }),
    onSuccess: () => client.invalidateQueries({ queryKey: ['tracking'] }),
    onError: (error: Error) => toast.push(error.message, 'error'),
  })
}

export function WantButton({ code }: { code: string }) {
  const tracking = useTracking()
  const change = useChange()
  const saved = tracking.data?.items.find((row) => row.kind === 'want' && keyOf(row.label) === keyOf(code))
  if (saved)
    return (
      <Link className="button button-secondary button-small" to="/tracking">
        {t('已加入想要')}
      </Link>
    )
  return (
    <Button
      size="small"
      disabled={change.isPending || !tracking.data}
      onClick={() => change.mutate({ action: 'save', kind: 'want', label: code })}
    >
      {t('加入想要')}
    </Button>
  )
}

export function WorkEditions({ code }: { code: string }) {
  const query = useQuery({
    queryKey: ['work-editions', code],
    queryFn: () => api.mediaLibrary({ query: code, limit: 500 }),
    enabled: Boolean(code),
    staleTime: 30000,
  })
  const editions =
    query.data?.items.filter((entry) => keyOf(entry.code || entry.code_key || '') === keyOf(code)) || []
  if (!editions.length) return null
  return (
    <section className="library-detail-files">
      <h2>{t('馆藏版本')}</h2>
      {editions.map((entry) => (
        <div className="library-detail-file" key={entry.entry_id}>
          <div>
            <Link to={'/library?entry=' + entry.entry_id}>
              {entry.variant ? webDownloadVariantLabel(entry.variant) : t('本地媒体')}
            </Link>
            <span> {entry.quality_height ? entry.quality_height + 'p' : t('画质未知')}</span>
            {entry.media_files.map((file, i) => (
              <div key={file.identity}>
                {entry.presence === 'present' ? (
                  <Link to={'/player?entry=' + entry.entry_id + '&file=' + i + '&identity=' + file.identity}>
                    {file.relative_path}
                  </Link>
                ) : (
                  <span>{file.relative_path}</span>
                )}{' '}
                {[file.part_key, file.quality_height && file.quality_height + 'p']
                  .filter(Boolean)
                  .join(' · ')}
              </div>
            ))}
          </div>
        </div>
      ))}
    </section>
  )
}

export function TrackingPage() {
  const [params] = useSearchParams()
  const person = params.get('person') || ''
  const tracking = useTracking()
  const change = useChange()
  const [kind, setKind] = useState<Kind>('want')
  const [label, setLabel] = useState('')
  const rows = (tracking.data?.items || []).filter(
    (row) =>
      !person ||
      (row.kind === 'person' &&
        [row.label, ...row.aliases].some((n) => n.toLowerCase() === person.toLowerCase())),
  )
  const names = [...new Set([person, ...rows.flatMap((row) => [row.label, ...row.aliases])])].filter(Boolean)
  const library = useQuery({
    queryKey: ['person-library', names],
    enabled: Boolean(person),
    queryFn: async () => {
      const pages = await Promise.all(names.map((actor) => api.mediaLibrary({ actor, limit: 50 })))
      return [
        ...new Map(pages.flatMap((page) => page.items).map((entry) => [entry.entry_id, entry])).values(),
      ]
    },
  })
  const states = {
    pending: t('等待查询'),
    found: t('找到候选'),
    empty: t('暂无匹配资源'),
    error: t('来源查询失败'),
  }
  return (
    <main className="page">
      <PageHeader title={person || t('想要与关注')} />
      {person ? (
        <div className="watch-inline-actions">
          <Link to="/tracking">{t('全部关注')}</Link>
          <Link to={'/library?actor=' + encodeURIComponent(person)}>{t('查看馆藏作品')}</Link>
          {!rows.length && tracking.data && (
            <Button
              disabled={change.isPending}
              onClick={() => change.mutate({ action: 'save', kind: 'person', label: person })}
            >
              {t('关注人物')}
            </Button>
          )}
        </div>
      ) : (
        <form
          className="watch-inline-actions"
          onSubmit={(event) => {
            event.preventDefault()
            change.mutate({ action: 'save', kind, label }, { onSuccess: () => setLabel('') })
          }}
        >
          <select aria-label={t('追踪类型')} value={kind} onChange={(e) => setKind(e.target.value as Kind)}>
            <option value="want">{t('想要作品')}</option>
            <option value="person">{t('关注人物')}</option>
          </select>
          <input
            required
            maxLength={80}
            aria-label={kind === 'want' ? t('番号') : t('演员')}
            placeholder={kind === 'want' ? t('番号') : t('演员')}
            value={label}
            onChange={(e) => setLabel(e.target.value)}
          />
          <Button disabled={change.isPending} type="submit">
            {t('添加')}
          </Button>
        </form>
      )}
      {tracking.isPending ? (
        <SkeletonRows />
      ) : tracking.isError ? (
        <InlineNotice tone="danger">{tracking.error.message}</InlineNotice>
      ) : (
        rows.map((row) => (
          <article className="library-detail-files" key={row.id}>
            <h2>
              {row.kind === 'person' ? (
                <Link to={'/tracking?person=' + encodeURIComponent(row.label)}>{row.label}</Link>
              ) : (
                row.label
              )}
            </h2>
            <p>
              {states[row.status]} {row.checked ? new Date(row.checked * 1000).toLocaleString() : ''}
            </p>
            <div className="watch-inline-actions">
              <Button
                disabled={change.isPending}
                onClick={() => change.mutate({ action: 'recheck', id: row.id })}
              >
                {t('重新查询')}
              </Button>
              <Button
                disabled={change.isPending}
                onClick={() => change.mutate({ action: 'remove', id: row.id })}
              >
                {t('移除追踪')}
              </Button>
            </div>
            <TrackingConditions row={row} />
            <ul>
              {row.results.map((r) => (
                <li key={r.code}>
                  <Link
                    to={'/works/' + encodeURIComponent(r.work_id) + '?code=' + encodeURIComponent(r.code)}
                  >
                    {r.code} · {r.title}
                  </Link>
                </li>
              ))}
            </ul>
          </article>
        ))
      )}
      {!person && tracking.data && !rows.length && <EmptyState title={t('还没有想要的作品或关注的人物')} />}
      {person && library.isError && (
        <InlineNotice tone="warning">
          {library.error.message} <Button onClick={() => void library.refetch()}>{t('重试')}</Button>
        </InlineNotice>
      )}
      {person && library.data && (
        <ul>
          {library.data.map((entry) => (
            <li key={entry.entry_id}>
              <Link to={'/library?entry=' + entry.entry_id}>
                {entry.code} · {entry.title}
              </Link>
            </li>
          ))}
        </ul>
      )}
    </main>
  )
}

function TrackingConditions({ row }: { row: Tracked }) {
  const change = useChange()
  return (
    <details>
      <summary>{row.kind === 'want' ? t('版本与画质要求') : t('人物别名')}</summary>
      <form
        className="watch-inline-actions"
        onSubmit={(event) => {
          event.preventDefault()
          const values = new FormData(event.currentTarget)
          change.mutate({
            action: 'save',
            kind: row.kind,
            label: row.label,
            variant: values.get('variant') || '',
            height: Number(values.get('height') || 0),
            aliases: String(values.get('aliases') || '')
              .split(/[,，]/)
              .map((s) => s.trim())
              .filter(Boolean),
          })
        }}
      >
        {row.kind === 'want' ? (
          <>
            <select name="variant" aria-label={t('版本')} defaultValue={row.variant}>
              <option value="">{t('全部版本')}</option>
              <option value="original">{t('原片')}</option>
              <option value="subtitle">{t('中文字幕')}</option>
              <option value="uncensored">{t('无码')}</option>
            </select>
            <select aria-label={t('最低画质')} name="height" defaultValue={row.height}>
              <option value="0">{t('不限画质')}</option>
              {[720, 1080, 2160].map((h) => (
                <option key={h} value={h}>
                  {h}p
                </option>
              ))}
            </select>
          </>
        ) : (
          <input
            name="aliases"
            aria-label={t('人物别名')}
            defaultValue={row.aliases.join(', ')}
            placeholder={t('多个别名用逗号分隔')}
          />
        )}
        <Button disabled={change.isPending} type="submit">
          {t('保存')}
        </Button>
      </form>
    </details>
  )
}
