import { ArrowRight } from 'lucide-react'
import type { MouseEvent, ReactNode } from 'react'
import { Link, useNavigate } from 'react-router-dom'

import { StatusBadge } from '../components/ui'
import { coverImageUrl } from '../lib/api'
import type { SiteSettings, WorkMagnet, WorkResult } from '../types'
import { sourceName, StableImage, type DownloadState, WorkMagnetList, workCover } from './WorkUi'

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

const INTERACTIVE_SELECTOR = 'a, button, input, select, textarea, label, summary, [role="button"]'

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
  const navigate = useNavigate()
  const code = result.code || result.canonical_code || '未知番号'
  const cover = workCover(result)
  const coverUrl = cover ? coverImageUrl(cover.sourceId, cover.url, sites) : ''

  // The cover, code, title and summary open the same detail page as 查看详情.
  // They stay plain content for assistive technology; the link remains the
  // single keyboard stop per work.
  function openFromRegion(event: MouseEvent<HTMLElement>) {
    const target = event.target as HTMLElement
    if (target.closest(INTERACTIVE_SELECTOR) || !target.closest('[data-open-detail]')) return
    if (globalThis.getSelection?.()?.toString().trim()) return
    if (event.ctrlKey || event.metaKey || event.shiftKey) {
      globalThis.open?.(detailHref, '_blank', 'noopener')
      return
    }
    navigate(detailHref, { state: detailState })
  }

  return (
    <article className="result-row result-row-openable" onClick={openFromRegion}>
      <div className="result-poster" data-open-detail>
        <StableImage src={coverUrl} alt={`${code} 封面`} referrerPolicy="no-referrer" />
      </div>
      <header className="result-heading">
        <div>
          <div className="result-code-line">
            <strong data-open-detail>{code}</strong>
            {result.sources.map((source) => <StatusBadge key={source.source_id}>{sourceName(source.source_id, sites)}</StatusBadge>)}
          </div>
          <h3 data-open-detail>{titleContent ?? result.title}</h3>
        </div>
        <div className="result-actions">
          {actions}
          <Link
            className="button button-secondary button-small result-detail-link"
            data-focus-return-key={`work:${result.work_id}`}
            to={detailHref}
            state={detailState}
          >
            查看详情
            <ArrowRight aria-hidden="true" />
          </Link>
        </div>
      </header>
      <div className="result-meta" data-open-detail>
        <span>{result.release_date || '日期未知'}</span>
        <span>
          {result.magnets.length
            ? `${result.magnets.length} 条磁链`
            : result.magnet_hint === 'available'
              ? '有磁链，等待解析'
              : result.magnet_hint === 'unavailable'
                ? '未标记磁链'
                : '磁链状态未知'}
        </span>
        {rating ? <span>{sourceName(rating.sourceId, sites)} 评分 {formatFivePointRating(rating.value)} / 5</span> : null}
        {result.release_date_conflict ? <span className="meta-warning">日期有差异</span> : null}
        {result.actors.slice(0, 3).map((actor) => <span key={actor}>{actor}</span>)}
        {result.actors.length > 3 ? <span>另 {result.actors.length - 3} 位</span> : null}
      </div>
      {result.tags.length ? <div className="result-tags" data-open-detail>{result.tags.slice(0, 6).map((tag) => <span key={tag}>{tag}</span>)}</div> : null}
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
