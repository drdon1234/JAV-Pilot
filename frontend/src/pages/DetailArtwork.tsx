import { type ImgHTMLAttributes, type KeyboardEvent, useEffect, useRef, useState } from 'react'
import { ChevronLeft, ChevronRight, ImageOff, X } from 'lucide-react'

import { EmptyState, IconButton, ProgressBar, SkeletonRows } from '../components/ui'
import { coverImageUrl } from '../lib/api'
import { t } from '../lib/i18n'
import type { SiteSettings, WorkImage, WorkResult, WorkSource } from '../types'
import { isVideoAssetUrl, sourceName } from './WorkUi'

type ImageState = 'loaded' | 'failed'

function imageUrls(sourceId: string, image: WorkImage, thumbnailFirst = false): string[] {
  const urls = thumbnailFirst ? [image.thumbnail_url, image.url] : [image.url, image.thumbnail_url]
  return urls.flatMap(url => url?.trim() && !isVideoAssetUrl(url) ? [coverImageUrl(sourceId, url)] : [])
}

// A partial stream can omit sites that are still being searched.
export function readyArtworkSources(work: WorkResult | undefined, order: string[], pending: boolean): WorkSource[] {
  if (!work) return []
  const byId = new Map(work.sources.map(source => [source.source_id, source]))
  const ready: WorkSource[] = []
  for (const id of new Set([...order, ...byId.keys()])) {
    const source = byId.get(id)
    if (source) ready.push(source)
    if (pending && (!source || source.parse_status === 'summary')) break
  }
  return ready
}

export function artworkUrls(sources: WorkSource[], cover: WorkResult['cover'], kind: 'cover' | 'backdrop'): string[] {
  const urls: string[] = []
  for (const source of sources) {
    if (kind === 'cover' && cover?.source_id === source.source_id) {
      urls.push(...imageUrls(source.source_id, cover))
    }
    const primary = source.images.filter(image => image.kind === kind)
    const alternate = kind === 'backdrop'
      ? source.images.filter(image => image.kind === 'sample').slice(0, 1)
      : []
    for (const image of [...primary, ...alternate]) urls.push(...imageUrls(source.source_id, image))
  }
  return [...new Set(urls)]
}

// The parent keys each slot by work or sample identity. A later metadata event
// can add candidates, but a decoded image stays mounted for that same slot.
export function DetailImage({ urls, retry, pending = false, onState, alt, ...props }: {
  urls: readonly string[]
  retry: number
  pending?: boolean
  onState?: (state: ImageState) => void
  alt: string
} & Omit<ImgHTMLAttributes<HTMLImageElement>, 'src' | 'onLoad' | 'onError'>) {
  const [loaded, setLoaded] = useState('')
  const [failures, setFailures] = useState<{ retry: number; urls: string[] }>({ retry, urls: [] })
  const failed = failures.retry === retry ? failures.urls : []
  const src = loaded || urls.find(url => !failed.includes(url))
  if (!src) {
    const label = pending ? t('正在解析') : urls.length ? t('加载失败') : t('暂无图片')
    return <div className="stable-image-placeholder" role="img" aria-label={`${alt} · ${label}`}>
      <ImageOff aria-hidden="true" /><span>{label}</span>
    </div>
  }

  return <img key={src} src={src} alt={alt} loading="lazy" decoding="async" referrerPolicy="no-referrer" {...props}
    onLoad={event => {
      if (!event.currentTarget.isConnected) return
      setLoaded(src)
      onState?.('loaded')
    }}
    onError={event => {
      if (!event.currentTarget.isConnected) return
      setLoaded('')
      const rejected = [...failed, src]
      setFailures({ retry, urls: rejected })
      if (urls.every(url => rejected.includes(url))) onState?.('failed')
    }}
  />
}

interface Gallery {
  sourceId: string
  images: WorkImage[]
}

function SourceGallery({ group, label, retry, states, mark }: {
  group: Gallery
  label: string
  retry: number
  states: Record<string, ImageState>
  mark: (source: string, url: string, state: ImageState) => void
}) {
  const { sourceId, images } = group
  const [openUrl, setOpenUrl] = useState('')
  const dialogRef = useRef<HTMLDialogElement>(null)
  const index = images.findIndex(image => image.url === openUrl)
  const selected = images[index]
  const failedCount = images.filter(image => states[image.url] === 'failed').length
  const settled = images.filter(image => states[image.url]).length
  useEffect(() => {
    const dialog = dialogRef.current
    if (selected && dialog && !dialog.open) dialog.showModal()
    if (!selected && dialog?.open) dialog.close()
  }, [selected])
  function move(offset: number) {
    setOpenUrl(images[(index + offset + images.length) % images.length].url)
  }
  function handleKey(event: KeyboardEvent<HTMLDialogElement>) {
    if (event.key === 'ArrowLeft') move(-1)
    if (event.key === 'ArrowRight') move(1)
  }
  return <>
    <div className="gallery-progress" aria-live="polite">
      <div>
        <span>{t('已处理 {settledImageCount} / {count}', { settledImageCount: settled, count: images.length })}</span>
        {failedCount > 0 && <span className="gallery-failure-count">{t('{failedImageCount} 张失败', { failedImageCount: failedCount })}</span>}
      </div>
      <ProgressBar value={settled / images.length} label={t('{sourceLabel} 截图加载进度', { sourceLabel: label })} />
    </div>
    <div className="source-gallery">
      {images.map((image, index) => {
        const alt = t('{sourceLabel} 截图 {value}', { sourceLabel: label, value: index + 1 })
        return <button key={image.url} type="button" className="gallery-item gallery-item-sample"
          aria-label={t('查看 {alt}', { alt })} onClick={() => setOpenUrl(image.url)}>
          <DetailImage urls={imageUrls(sourceId, image, true)} alt={alt} retry={retry}
            onState={state => mark(sourceId, image.url, state)} />
        </button>
      })}
    </div>
    {openUrl && <dialog ref={dialogRef} className="image-lightbox" aria-label={t('图片查看器')}
      onClose={() => setOpenUrl('')} onKeyDown={handleKey}
      onClick={event => { if (event.target === event.currentTarget) setOpenUrl('') }}>
      {selected && <div className="lightbox-content">
        <div className="lightbox-toolbar">
          <span>{index + 1} / {images.length}</span>
          <IconButton label={t('关闭图片查看器')} onClick={() => setOpenUrl('')} autoFocus><X aria-hidden="true" /></IconButton>
        </div>
        <div className="lightbox-stage">
          <IconButton label={t('上一张')} onClick={() => move(-1)} disabled={images.length <= 1}><ChevronLeft aria-hidden="true" /></IconButton>
          <DetailImage key={selected.url} urls={imageUrls(sourceId, selected)} retry={retry} loading="eager"
            alt={t('{value} 截图 {value2}', { value: label, value2: index + 1 })} />
          <IconButton label={t('下一张')} onClick={() => move(1)} disabled={images.length <= 1}><ChevronRight aria-hidden="true" /></IconButton>
        </div>
      </div>}
    </dialog>}
  </>
}

export function DetailGalleries({ active, samples, sites, pending, retry }: {
  active: WorkSource
  samples: ReadonlyMap<string, WorkImage[]>
  sites: readonly SiteSettings[]
  pending: boolean
  retry: number
}) {
  const [states, setStates] = useState<Record<string, Record<string, ImageState>>>({})
  function mark(source: string, url: string, state: ImageState) {
    setStates(current => current[source]?.[url] === state ? current : {
      ...current, [source]: { ...current[source], [url]: state },
    })
  }

  const groups: Gallery[] = []
  const seen = new Set<string>()
  const sourceIds = new Set([active.source_id, ...samples.keys()])
  for (const sourceId of sourceIds) {
    const images = (samples.get(sourceId) ?? []).filter(image => !seen.has(image.url))
    if (!images.length) continue
    groups.push({ sourceId, images })
    // Keep working screenshots. If part of a gallery fails, supplement it
    // with the next site's own album without assuming their ordering matches.
    if (!images.some(image => states[sourceId]?.[image.url] === 'failed')) break
    for (const image of images) {
      if (states[sourceId]?.[image.url] !== 'failed') seen.add(image.url)
    }
  }
  if (!groups.length) return pending ? <SkeletonRows count={1} /> : <EmptyState className="compact-empty" title={t('该站点暂无截图')} />
  return groups.map(group => {
    const label = sourceName(group.sourceId, sites)
    return <div key={group.sourceId}>
      {group.sourceId !== active.source_id && <h3>{t('截图来源：{sourceLabel}', { sourceLabel: label })}</h3>}
      <SourceGallery group={group} label={label} retry={retry} states={states[group.sourceId] ?? {}} mark={mark} />
    </div>
  })
}
