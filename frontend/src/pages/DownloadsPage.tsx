import { Archive, Globe2, Magnet } from 'lucide-react'
import { lazy, Suspense, useEffect, useRef, type KeyboardEvent } from 'react'
import { useSearchParams } from 'react-router-dom'

import { PageHeader, SkeletonRows } from '../components/ui'
import { recoverableImport } from '../lib/recoverableImport'
import { TorrentDownloadsView } from './TorrentDownloadsView'

import '../styles/downloads.css'

const WebDownloadsView = lazy(() => recoverableImport('src/pages/WebDownloadsView.tsx', 'WebDownloadsView', () => import('./WebDownloadsView')).then((module) => ({
  default: module.WebDownloadsView,
})))

const FailedDownloadArchiveView = lazy(() => recoverableImport('src/pages/FailedDownloadArchiveView.tsx', 'FailedDownloadArchiveView', () => import('./FailedDownloadArchiveView')).then((module) => ({
  default: module.FailedDownloadArchiveView,
})))

const downloadViews = [
  { id: 'bt', label: 'BT 下载', panelId: 'bt-download-panel', Icon: Magnet },
  { id: 'web', label: 'Web 下载', panelId: 'web-download-panel', Icon: Globe2 },
  { id: 'archive', label: '失败归档', panelId: 'archive-download-panel', Icon: Archive },
] as const

type DownloadView = typeof downloadViews[number]['id']

export function DownloadsPage() {
  const [searchParams, setSearchParams] = useSearchParams()
  const pendingTabFocus = useRef<DownloadView | null>(null)
  const requestedView = searchParams.get('view')
  const downloadView: DownloadView = requestedView === 'web' || requestedView === 'archive'
    ? requestedView
    : 'bt'

  function selectDownloadView(view: DownloadView) {
    setSearchParams(view === 'bt' ? {} : { view }, { replace: true })
  }

  useEffect(() => {
    if (pendingTabFocus.current !== downloadView) return
    document.getElementById(`download-view-${downloadView}`)?.focus()
    pendingTabFocus.current = null
  }, [downloadView])

  function handleViewTabKeyDown(event: KeyboardEvent<HTMLButtonElement>, view: DownloadView) {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(event.key)) return
    event.preventDefault()
    const currentIndex = downloadViews.findIndex((item) => item.id === view)
    const nextIndex = event.key === 'Home'
      ? 0
      : event.key === 'End'
        ? downloadViews.length - 1
        : event.key === 'ArrowLeft'
          ? (currentIndex - 1 + downloadViews.length) % downloadViews.length
          : (currentIndex + 1) % downloadViews.length
    const nextView = downloadViews[nextIndex].id
    if (nextView === view) {
      event.currentTarget.focus()
      return
    }
    pendingTabFocus.current = nextView
    selectDownloadView(nextView)
  }

  const navigation = (
    <div className="download-view-tabs" role="tablist" aria-label="下载任务视图">
      {downloadViews.map(({ id, label, panelId, Icon }) => (
        <button
          type="button"
          role="tab"
          id={`download-view-${id}`}
          aria-controls={panelId}
          aria-selected={downloadView === id}
          tabIndex={downloadView === id ? 0 : -1}
          className={downloadView === id ? 'active' : ''}
          onClick={() => selectDownloadView(id)}
          onKeyDown={(event) => handleViewTabKeyDown(event, id)}
          key={id}
        >
          <Icon aria-hidden="true" />
          {label}
        </button>
      ))}
    </div>
  )

  return (
    <div className="page downloads-page">
      <TorrentDownloadsView active={downloadView === 'bt'} navigation={navigation} />
      {downloadView === 'web' ? (
        <>
          <PageHeader title="下载任务" description="BT、Web 下载队列与失败任务归档" />
          {navigation}
          <Suspense
            fallback={(
              <div id="web-download-panel" role="tabpanel" aria-labelledby="download-view-web" tabIndex={0}>
                <SkeletonRows count={6} />
              </div>
            )}
          >
            <WebDownloadsView />
          </Suspense>
        </>
      ) : null}
      {downloadView === 'archive' ? (
        <>
          <PageHeader title="下载任务" description="BT、Web 下载队列与失败任务归档" />
          {navigation}
          <Suspense
            fallback={(
              <div id="archive-download-panel" role="tabpanel" aria-labelledby="download-view-archive" tabIndex={0}>
                <SkeletonRows count={6} />
              </div>
            )}
          >
            <FailedDownloadArchiveView />
          </Suspense>
        </>
      ) : null}
    </div>
  )
}
