import { ArrowRight } from 'lucide-react'
import type { ReactNode } from 'react'
import { Link } from 'react-router-dom'

import { StatusBadge } from '../components/ui'
import { WatchLink } from '../components/WatchActions'
import { coverImageUrl } from '../lib/api'
import type { SiteSettings, WorkMagnet, WorkResult } from '../types'
import { sourceName, StableImage, type DownloadState, WorkMagnetList, workCover } from './WorkUi'
import { t } from '../lib/i18n'

export interface SearchResultCardProps {
  result: WorkResult
  sites: readonly SiteSettings[]
  detailHref: string
  detailState: Record<string, unknown>
  rating: { sourceId: string; value: number } | null
  downloadStates: Record<string, DownloadState>
  onDownload: (magnet: WorkMagnet) => void
  onOpenDownloads: () => void
  /** Extra actions beside 查看详情, such as 从 Web 下载. */
  actions?: ReactNode
  /** Replaces the title text, e.g. with a translated title. */
  titleContent?: ReactNode
  extra?: ReactNode
}

function formatFivePointRating(value: number): string {
  return value.toFixed(2).replace(/\.?0+$/, '')
}

export function SearchResultCard({
  result,
  sites,
  detailHref,
  detailState,
  rating,
  downloadStates,
  onDownload,
  onOpenDownloads,
  actions,
  titleContent,
  extra,
}: SearchResultCardProps) {
  const code = result.code || result.canonical_code || t('未知番号')
  const cover = workCover(result)
  const coverUrl = cover ? coverImageUrl(cover.sourceId, cover.url, sites) : ''

  return (
    <article className="result-row">
      <Link className="result-poster result-detail-content" to={detailHref} state={detailState} tabIndex={-1}>
        <StableImage src={coverUrl} alt={t('{code} 封面', { code })} referrerPolicy="no-referrer" />
      </Link>
      <header className="result-heading">
        <div>
          <div className="result-code-line">
            <strong><Link className="result-detail-content" to={detailHref} state={detailState} tabIndex={-1}>{code}</Link></strong>
            {result.sources.map((source) => <StatusBadge key={source.source_id}>{sourceName(source.source_id, sites)}</StatusBadge>)}
          </div>
          <h3><Link className="result-detail-content" to={detailHref} state={detailState} tabIndex={-1}>{titleContent ?? result.title}</Link></h3>
        </div>
        <div className="result-actions">
          {result.code || result.canonical_code ? <WatchLink code={result.code || result.canonical_code || ''} /> : null}
          {actions}
          <Link
            className="button button-secondary button-small result-detail-link"
            data-focus-return-key={`work:${result.work_id}`}
            to={detailHref}
            state={detailState}
          >
            {t('查看详情')}
            <ArrowRight aria-hidden="true" />
          </Link>
        </div>
      </header>
      <Link className="result-meta result-detail-content" to={detailHref} state={detailState} tabIndex={-1}>
        <span>{result.release_date || t('日期未知')}</span>
        <span>
          {result.magnets.length
            ? t('{count} 条磁链', { count: result.magnets.length })
            : result.magnet_hint === 'available'
              ? t('有磁链，等待解析')
              : result.magnet_hint === 'unavailable'
                ? t('未标记磁链')
                : t('磁链状态未知')}
        </span>
        {rating ? <span>{t('{value} 评分 {fivePointRating} / 5', { value: sourceName(rating.sourceId, sites), fivePointRating: formatFivePointRating(rating.value) })}</span> : null}
        {result.release_date_conflict ? <span className="meta-warning">{t('日期有差异')}</span> : null}
        {result.actors.slice(0, 3).map((actor) => <span key={actor}>{actor}</span>)}
        {result.actors.length > 3 ? <span>{t('另 {value} 位', { value: result.actors.length - 3 })}</span> : null}
      </Link>
      {result.tags.length ? <Link className="result-tags result-detail-content" to={detailHref} state={detailState} tabIndex={-1}>{result.tags.slice(0, 6).map((tag) => <span key={tag}>{tag}</span>)}</Link> : null}
      {extra}
      <WorkMagnetList
        magnets={result.magnets}
        sites={sites}
        initialLimit={3}
        states={downloadStates}
        onDownload={onDownload}
        onOpenDownloads={onOpenDownloads}
      />
    </article>
  )
}
