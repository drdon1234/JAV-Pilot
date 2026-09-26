import { Check, Download, ImageOff, LoaderCircle } from 'lucide-react'
import { type ImgHTMLAttributes, useEffect, useRef, useState } from 'react'

import { CopyTextButton } from '../components/CopyTextButton'
import { Button, StatusBadge } from '../components/ui'
import { formatBytes } from '../lib/format'
import type { MagnetProbeItem, MagnetSelectionItem, SiteSettings, WorkImage, WorkMagnet, WorkResult } from '../types'

export type DownloadState = 'adding' | 'added' | 'error'

export interface CanonicalMagnetAction {
  primary: WorkMagnet['source_refs'][number] | null
  trackers: string[]
  uri: string
}

function trackerParams(uri: string): string[] {
  try {
    const parsed = new URL(uri)
    return parsed.protocol === 'magnet:' ? parsed.searchParams.getAll('tr') : []
  } catch {
    return []
  }
}

function queryParamName(segment: string): string {
  const separator = segment.indexOf('=')
  const encoded = separator < 0 ? segment : segment.slice(0, separator)
  try {
    return decodeURIComponent(encoded.replace(/\+/g, ' ')).toLowerCase()
  } catch {
    return encoded.toLowerCase()
  }
}

function replaceTrackers(uri: string, trackers: readonly string[]): string {
  if (!uri || !trackers.length) return uri
  const fragmentIndex = uri.indexOf('#')
  const fragment = fragmentIndex < 0 ? '' : uri.slice(fragmentIndex)
  const withoutFragment = fragmentIndex < 0 ? uri : uri.slice(0, fragmentIndex)
  const queryIndex = withoutFragment.indexOf('?')
  const base = queryIndex < 0 ? withoutFragment : withoutFragment.slice(0, queryIndex)
  const rawQuery = queryIndex < 0 ? '' : withoutFragment.slice(queryIndex + 1)
  const preserved = rawQuery
    .split('&')
    .filter((segment) => segment && queryParamName(segment) !== 'tr')
  const merged = [...preserved, ...trackers.map((tracker) => `tr=${encodeURIComponent(tracker)}`)]
  return `${base}?${merged.join('&')}${fragment}`
}

export function canonicalMagnetAction(magnet: WorkMagnet): CanonicalMagnetAction {
  const primary = magnet.source_refs[0] ?? null
  if (!primary) return { primary: null, trackers: [], uri: '' }
  const trackers: string[] = []
  const seen = new Set<string>()
  const append = (value: string) => {
    const tracker = value.trim()
    if (!tracker || seen.has(tracker)) return
    seen.add(tracker)
    trackers.push(tracker)
  }
  magnet.source_refs.forEach((source) => {
    trackerParams(source.uri).forEach(append)
    source.trackers.forEach(append)
  })
  return { primary, trackers, uri: replaceTrackers(primary.uri, trackers) }
}

export function sourceName(sourceId: string, sites: readonly SiteSettings[]): string {
  return sites.find((site) => site.id === sourceId)?.name || sourceId
}

export function workCover(work: WorkResult, preferOriginal = false): { url: string; sourceId: string } | null {
  if (work.cover && !isVideoAssetUrl(work.cover.url) && !isVideoAssetUrl(work.cover.thumbnail_url)) {
    return { url: preferOriginal ? work.cover.url : work.cover.thumbnail_url || work.cover.url, sourceId: work.cover.source_id }
  }
  for (const source of work.sources) {
    const cover = source.images.find((image) => image.kind === 'cover' && !isVideoAssetUrl(image.url) && !isVideoAssetUrl(image.thumbnail_url))
    if (cover) return { url: preferOriginal ? cover.url : cover.thumbnail_url || cover.url, sourceId: source.source_id }
  }
  return null
}

function isVideoAssetUrl(value: string | null | undefined): boolean {
  const candidate = value?.trim()
  if (!candidate) return false
  if (candidate.toLowerCase().startsWith('data:video/')) return true
  try {
    const pathname = new URL(candidate, 'https://image.invalid').pathname.toLowerCase()
    return /\.(?:avi|flv|m3u8|mkv|mov|mp4|webm|wmv)$/.test(pathname)
  } catch {
    return /\.(?:avi|flv|m3u8|mkv|mov|mp4|webm|wmv)(?:$|[?#])/i.test(candidate)
  }
}

function imageIdentity(value: string): string {
  const candidate = value.trim()
  try {
    const parsed = new URL(candidate)
    parsed.hash = ''
    return parsed.href
  } catch {
    return candidate
  }
}

export function workBackdrop(work: WorkResult, preferOriginal = false): { url: string; sourceId: string } | null {
  for (const source of work.sources) {
    const backdrop = source.images.find((image) => image.kind === 'backdrop' && image.url && !isVideoAssetUrl(image.url) && !isVideoAssetUrl(image.thumbnail_url))
    if (backdrop) return { url: preferOriginal ? backdrop.url : backdrop.thumbnail_url || backdrop.url, sourceId: source.source_id }
  }
  return null
}

export function uniqueSampleImages(images: readonly WorkImage[]): WorkImage[] {
  const seen = new Set<string>()
  return images.filter((image) => {
    if (image.kind !== 'sample' || !image.url.trim() || isVideoAssetUrl(image.url) || isVideoAssetUrl(image.thumbnail_url)) return false
    const identities = [image.url, image.thumbnail_url]
      .filter((value): value is string => Boolean(value?.trim()))
      .map(imageIdentity)
    if (identities.some((identity) => seen.has(identity))) return false
    identities.forEach((identity) => seen.add(identity))
    return true
  })
}

export function StableImage({
  src,
  alt,
  className = '',
  emptyLabel = '暂无图片',
  retryToken = 0,
  onLoad,
  onError,
  ...props
}: ImgHTMLAttributes<HTMLImageElement> & {
  src: string
  alt: string
  emptyLabel?: string
  retryToken?: string | number
}) {
  const retryTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  const [state, setState] = useState<{
    attempt: number
    phase: 'loading' | 'waiting' | 'failed'
    src: string
    retryToken: string | number
  }>(() => ({ attempt: 0, phase: 'loading', src, retryToken }))
  const currentState = state.src === src && state.retryToken === retryToken
    ? state
    : { attempt: 0, phase: 'loading' as const, src, retryToken }

  useEffect(() => {
    if (retryTimer.current !== null) {
      clearTimeout(retryTimer.current)
      retryTimer.current = null
    }
    setState((current) => (
      current.src === src
      && current.retryToken === retryToken
      && current.attempt === 0
      && current.phase === 'loading'
        ? current
        : { attempt: 0, phase: 'loading', src, retryToken }
    ))
    return () => {
      if (retryTimer.current !== null) {
        clearTimeout(retryTimer.current)
        retryTimer.current = null
      }
    }
  }, [retryToken, src])

  if (!src || currentState.phase !== 'loading') {
    const waiting = currentState.phase === 'waiting'
    return (
      <div
        className={`stable-image-placeholder ${className}`.trim()}
        role="img"
        aria-label={!src ? `${alt}不可用` : waiting ? `${alt}重新加载中` : `${alt}加载失败`}
      >
        <ImageOff aria-hidden="true" />
        <span>{!src ? emptyLabel : waiting ? '重新加载中' : '加载失败'}</span>
      </div>
    )
  }

  return (
    <img
      key={`${currentState.src}:${String(currentState.retryToken)}:${currentState.attempt}`}
      src={currentState.src}
      alt={alt}
      className={className}
      loading="lazy"
      decoding="async"
      {...props}
      onLoad={(event) => {
        if (retryTimer.current !== null) {
          clearTimeout(retryTimer.current)
          retryTimer.current = null
        }
        onLoad?.(event)
      }}
      onError={(event) => {
        const retryDelays = [400, 1_200] as const
        const retryDelay = retryDelays[currentState.attempt]
        if (retryDelay !== undefined) {
          const failedAttempt = currentState.attempt
          const failedSource = currentState.src
          const failedRetryToken = currentState.retryToken
          setState({
            attempt: failedAttempt,
            phase: 'waiting',
            src: failedSource,
            retryToken: failedRetryToken,
          })
          retryTimer.current = setTimeout(() => {
            retryTimer.current = null
            setState((latest) => (
              latest.src === failedSource
              && latest.retryToken === failedRetryToken
              && latest.attempt === failedAttempt
              && latest.phase === 'waiting'
                ? {
                    attempt: failedAttempt + 1,
                    phase: 'loading',
                    src: failedSource,
                    retryToken: failedRetryToken,
                  }
                : latest
            ))
          }, retryDelay)
          return
        }
        setState({ ...currentState, phase: 'failed' })
        onError?.(event)
      }}
    />
  )
}

export function WorkMagnetList({
  magnets,
  sites,
  initialLimit,
  states,
  downloadDisabled = false,
  onDownload,
  onOpenDownloads,
  probeItems,
  selectionItems,
}: {
  magnets: WorkMagnet[]
  sites: readonly SiteSettings[]
  initialLimit?: number
  states: Record<string, DownloadState>
  downloadDisabled?: boolean
  onDownload: (magnet: WorkMagnet) => void
  onOpenDownloads: () => void
  probeItems?: Record<string, MagnetProbeItem>
  selectionItems?: Record<string, MagnetSelectionItem>
}) {
  const [expanded, setExpanded] = useState(false)
  const visible = initialLimit && !expanded ? magnets.slice(0, initialLimit) : magnets

  if (!magnets.length) return <div className="magnet-empty">暂未发现磁链</div>

  return (
    <div className="work-magnet-list">
      {visible.map((magnet) => {
        const action = canonicalMagnetAction(magnet)
        const primary = action.primary
        const state = states[magnet.info_hash]
        const badges = Array.from(new Set(magnet.source_refs.flatMap((source) => source.badges)))
        const sourceIds = Array.from(new Set(magnet.source_refs.map((source) => source.source_id)))
        const probe = probeItems?.[magnet.info_hash]
        const selection = selectionItems?.[magnet.info_hash]
        const accessibleName = magnet.display_name || primary?.display_name || magnet.info_hash.slice(0, 12)
        return (
          <div className="work-magnet-row" key={magnet.info_hash}>
            <div className="work-magnet-main">
              <strong title={magnet.display_name || primary?.display_name || magnet.info_hash}>
                {magnet.display_name || primary?.display_name || magnet.info_hash}
              </strong>
              <div className="work-magnet-meta">
                <span>{magnet.size_bytes ? `${magnet.size_is_exact === false ? '约 ' : ''}${formatBytes(magnet.size_bytes)}` : primary?.reported_size_text || '大小未知'}</span>
                {action.trackers.length ? <span>{action.trackers.length} 个 Tracker</span> : null}
                {sourceIds.map((sourceId) => <StatusBadge key={sourceId}>{sourceName(sourceId, sites)}</StatusBadge>)}
                {magnet.source_refs.filter((source) => source.reported_seeders != null).map((source) => (
                  <span key={`${source.source_id}:activity`} title={`索引站报告，非 qB 实测；观测时间：${source.reported_at ?? '未知'}`}>
                    {sourceName(source.source_id, sites)} 报告 {source.reported_seeders} 做种
                    {source.reported_leechers != null ? ` / ${source.reported_leechers} 下载` : ''}
                  </span>
                ))}
                {badges.map((badge) => <StatusBadge tone="info" key={badge}>{badge}</StatusBadge>)}
                {probe ? (
                  <StatusBadge tone={probe.seed_status === 'available' ? 'success' : probe.seed_status === 'none_observed' ? 'warning' : 'neutral'}>
                    {probe.seed_status === 'available'
                      ? `${Math.max(0, probe.seeders ?? 0, probe.connected_seeders ?? 0) || 1} 个做种`
                      : probe.seed_status === 'none_observed'
                        ? '暂未发现做种'
                        : '观察不充分'}
                  </StatusBadge>
                ) : null}
                {probe?.origin === 'preexisting' ? <StatusBadge tone="info">qB 已存在</StatusBadge> : null}
                {selection?.quality_label ? <StatusBadge tone="info">画质 {selection.quality_label}</StatusBadge> : null}
                {selection && ((selection.seeders ?? 0) > 0 || (selection.connected_seeders ?? 0) > 0) ? (
                  <StatusBadge tone="success">
                    {Math.max(selection.seeders ?? 0, selection.connected_seeders ?? 0)} 个做种
                  </StatusBadge>
                ) : null}
                {selection && (selection.download_speed ?? 0) > 0 ? (
                  <StatusBadge tone="neutral">{formatBytes(selection.download_speed ?? 0, true)}</StatusBadge>
                ) : null}
                {selection?.selected ? <StatusBadge tone="success">智能选中</StatusBadge> : null}
              </div>
            </div>
            <div className="work-magnet-actions">
              <CopyTextButton text={action.uri} label={`复制磁链：${accessibleName}`} />
              <Button
                type="button"
                size="small"
                variant={state === 'added' ? 'secondary' : 'primary'}
                disabled={!action.uri || state === 'adding' || downloadDisabled}
                onClick={state === 'added' ? onOpenDownloads : () => onDownload(magnet)}
                aria-label={`${state === 'adding' ? '正在添加' : state === 'added' ? '查看任务' : '下载'}：${accessibleName}`}
              >
                {state === 'adding' ? <LoaderCircle className="spin" aria-hidden="true" /> : state === 'added' ? <Check aria-hidden="true" /> : <Download aria-hidden="true" />}
                {state === 'adding' ? '添加中' : state === 'added' ? '查看任务' : '下载'}
              </Button>
            </div>
          </div>
        )
      })}
      {initialLimit && magnets.length > initialLimit ? (
        <Button type="button" variant="ghost" size="small" className="magnet-expand" onClick={() => setExpanded((value) => !value)}>
          {expanded ? '收起磁链' : `展开全部 ${magnets.length} 条磁链`}
        </Button>
      ) : null}
    </div>
  )
}
