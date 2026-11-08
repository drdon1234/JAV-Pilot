import { useEffect, useState } from 'react'
import { useQuery } from '@tanstack/react-query'
import { Link, useLocation } from 'react-router-dom'
import { Captions, FileImage, Film, Play } from 'lucide-react'
import { BackLink } from './BackLink'
import { requestJson } from '../lib/api'
import { t } from '../lib/i18n'
import { webDownloadVariantLabel } from '../lib/webDownloads'
import type { MediaLibraryEntry } from '../types'
import { SubtitleCandidatesDialog, type SubtitleDialogTarget } from './SubtitleCandidatesDialog'
import { Button, EmptyState, InlineNotice, PageHeader, SkeletonRows, StatusBadge } from './ui'

export function LibraryPoster({ entry }: { entry: MediaLibraryEntry }) {
  const [failed, setFailed] = useState(false)
  const url = `/api/library/artwork?${new URLSearchParams({ entry: entry.entry_id, revision: String(entry.revision) })}`
  useEffect(() => setFailed(false), [url])
  return <div className="library-poster">
    {entry.portrait_status === 'present' && entry.presence === 'present' && !failed
      ? <img src={url} alt={entry.title || entry.code || t('作品封面')} loading="lazy" decoding="async" onError={() => setFailed(true)} />
      : <div className="library-poster-placeholder"><Film aria-hidden="true" /><strong>{entry.code || t('本地媒体')}</strong><span>{t('暂无海报')}</span></div>}
  </div>
}

export function LibraryDetail({ entryId }: { entryId: string }) {
  const location = useLocation()
  const returnPath = location.state?.libraryReturnPath || '/library'
  const query = useQuery({ queryKey: ['library-entry', entryId], queryFn: ({ signal }) => requestJson<{item: MediaLibraryEntry}>(`/api/library?${new URLSearchParams({ entry_id: entryId })}`, { signal }), retry: false })
  const [subtitle, setSubtitle] = useState<SubtitleDialogTarget | null>(null)
  const entry = query.data?.item
  const currentPath = `${location.pathname}${location.search}`
  return <div className="page library-page library-detail-page">
    <BackLink fallback={returnPath} />
    <PageHeader title={entry?.code || t('作品详情')} />
    {query.isPending ? <SkeletonRows count={4} /> : query.isError ? <EmptyState title={t('无法加载媒体库')} description={t('无法读取媒体库，请稍后重试。')} action={<Button onClick={() => void query.refetch()}>{t('重试')}</Button>} /> : null}
    {entry ? <>
      <section className="library-detail-main">
        <LibraryPoster entry={entry} />
        <div className="library-detail-body">
          <div className="watch-inline-actions">
            {entry.variant ? <StatusBadge tone="info">{webDownloadVariantLabel(entry.variant)}</StatusBadge> : null}
            {entry.quality_height ? <StatusBadge>{entry.quality_height}p</StatusBadge> : null}
            <StatusBadge tone={entry.presence === 'present' ? 'success' : 'warning'}>{entry.presence === 'present' ? t('文件存在') : entry.presence === 'missing' ? t('文件已删除') : t('存储待确认')}</StatusBadge>
          </div>
          <h2>{entry.title || entry.code || t('本地媒体')}</h2>
          <div className="library-detail-actions">
            {entry.presence === 'present' && entry.media_paths.length ? <Link className="button button-primary" to={`/player?entry=${encodeURIComponent(entry.entry_id)}`}><Play aria-hidden="true" />{t('本地播放')}</Link> : null}
            {entry.code && entry.presence === 'present' ? <Link className="button button-secondary" to={`/metadata?${new URLSearchParams({ code: entry.code, return: currentPath })}`} state={{ libraryReturnPath: currentPath }}><FileImage aria-hidden="true" />{t('补全元数据')}</Link> : null}
            <Button disabled={!entry.code || entry.presence !== 'present'} onClick={() => setSubtitle({ kind: 'entry', entryId: entry.entry_id, code: entry.code || '' })}><Captions aria-hidden="true" />{t('字幕')}</Button>
          </div>
          {entry.presence !== 'present' ? <InlineNotice tone="warning">{t('本地文件不可用，请检查存储连接后重试。')}</InlineNotice> : null}
          <dl className="library-detail-facts">
            <Fact label={t('发行日期')} values={entry.release_date ? [entry.release_date] : []} />
            <Fact label={t('演员')} values={entry.actors} filter="actor" />
            <Fact label={t('片商')} values={[...new Set([...entry.makers, ...entry.publishers])]} filter="maker" />
            <Fact label={t('导演')} values={entry.directors} />
            <Fact label={t('系列')} values={entry.series} filter="series" />
            <Fact label={t('标签')} values={entry.tags} filter="tag" />
          </dl>
          {entry.code ? <Link className="library-text-link" to={`/library?${new URLSearchParams({ group: entry.code.replace(/[-_ ]?\d+$/, '').replace(/[-_ ]+$/, '') })}`} state={{ libraryReturnPath: '/library' }}>{t('查看同系列目录')}</Link> : null}
        </div>
      </section>
      <section className="library-detail-files"><h2>{t('视频文件')}</h2>
        {entry.media_paths.map((path, index) => <div className="library-detail-file" key={path}><div><strong>{t('视频文件')} {index + 1}</strong><code>{path}</code></div>
          {entry.presence === 'present' ? <Link className="button button-secondary" to={`/player?${new URLSearchParams({ entry: entry.entry_id, file: String(index) })}`}><Play aria-hidden="true" />{t('播放')}</Link> : null}
        </div>)}
        <div className="watch-inline-actions">{[
          ['NFO', entry.nfo_status], [t('竖图'), entry.portrait_status], [t('横图'), entry.landscape_status], [t('字幕'), entry.subtitle_status],
        ].map(([label, status]) => <StatusBadge key={label} tone={status === 'present' ? 'success' : 'warning'}>{status === 'present' ? t('{name} 完整', { name: label }) : t('{name} 缺失', { name: label })}</StatusBadge>)}</div>
      </section>
    </> : null}
    <SubtitleCandidatesDialog target={subtitle} onClose={() => setSubtitle(null)} />
  </div>
}

function Fact({ label, values, filter }: { label: string; values: string[]; filter?: string }) {
  if (!values.length) return null
  return <div><dt>{label}</dt><dd>{values.map((value, index) => <span key={`${value}:${index}`}>{filter ? <Link to={`/library?${new URLSearchParams({ [filter]: value })}`}>{value}</Link> : value}</span>)}</dd></div>
}
