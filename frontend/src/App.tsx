import { useQuery } from '@tanstack/react-query'
import { RefreshCw } from 'lucide-react'
import { Component, lazy, Suspense, type ErrorInfo, type ReactNode } from 'react'
import { Navigate, Outlet, Route, Routes } from 'react-router-dom'

import { AppShell } from './components/AppShell'
import { Button, EmptyState } from './components/ui'
import { api } from './lib/api'
import { recoverableImport } from './lib/recoverableImport'
import { SearchSessionProvider } from './lib/searchSessions'
import { t } from './lib/i18n'

const DownloadsPage = lazy(() => recoverableImport('src/pages/DownloadsPage.tsx', 'DownloadsPage', () => import('./pages/DownloadsPage')).then((module) => ({ default: module.DownloadsPage })))
const DetailPage = lazy(() => recoverableImport('src/pages/DetailPage.tsx', 'DetailPage', () => import('./pages/DetailPage')).then((module) => ({ default: module.DetailPage })))
const LibraryPage = lazy(() => recoverableImport('src/pages/LibraryPage.tsx', 'LibraryPage', () => import('./pages/LibraryPage')).then((module) => ({ default: module.LibraryPage })))
const LoginPage = lazy(() => recoverableImport('src/pages/LoginPage.tsx', 'LoginPage', () => import('./pages/LoginPage')).then((module) => ({ default: module.LoginPage })))
const MetadataPage = lazy(() => recoverableImport('src/pages/MetadataPage.tsx', 'MetadataPage', () => import('./pages/MetadataPage')).then((module) => ({ default: module.MetadataPage })))
const OrganizerPage = lazy(() => recoverableImport('src/pages/OrganizerPage.tsx', 'OrganizerPage', () => import('./pages/OrganizerPage')).then((module) => ({ default: module.OrganizerPage })))
const SearchPage = lazy(() => recoverableImport('src/pages/SearchPage.tsx', 'SearchPage', () => import('./pages/SearchPage')).then((module) => ({ default: module.SearchPage })))
const SettingsPage = lazy(() => recoverableImport('src/pages/SettingsPage.tsx', 'SettingsPage', () => import('./pages/SettingsPage')).then((module) => ({ default: module.SettingsPage })))
const WorkflowDefaultsPage = lazy(() => recoverableImport('src/pages/WorkflowDefaultsPage.tsx', 'WorkflowDefaultsPage', () => import('./pages/WorkflowDefaultsPage')).then((module) => ({ default: module.WorkflowDefaultsPage })))
const SearchHistoryPage = lazy(() => recoverableImport('src/pages/SearchHistoryPage.tsx', 'SearchHistoryPage', () => import('./pages/SearchHistoryPage')).then((module) => ({ default: module.SearchHistoryPage })))
const RankingsPage = lazy(() => recoverableImport('src/pages/RankingsPage.tsx', 'RankingsPage', () => import('./pages/RankingsPage')).then((module) => ({ default: module.RankingsPage })))
const SitesPage = lazy(() => recoverableImport('src/pages/SitesPage.tsx', 'SitesPage', () => import('./pages/SitesPage')).then((module) => ({ default: module.SitesPage })))
const SubtitlesPage = lazy(() => recoverableImport('src/pages/SubtitlesPage.tsx', 'SubtitlesPage', () => import('./pages/SubtitlesPage')).then((module) => ({ default: module.SubtitlesPage })))

function lazyRoute(element: ReactNode) {
  return <Suspense fallback={<div className="app-loading" role="status" aria-label={t('正在加载页面')} />}>{element}</Suspense>
}

function AuthGate() {
  const auth = useQuery({ queryKey: ['auth'], queryFn: api.authStatus, staleTime: 30_000 })
  if (auth.isLoading) return <div className="app-loading" role="status" aria-label={t('正在验证登录状态')} />
  if (auth.isError) {
    return (
      <EmptyState
        title={t('无法验证登录状态')}
        description={(auth.error as Error).message}
        action={(
          <Button type="button" onClick={() => void auth.refetch()} disabled={auth.isFetching}>
            <RefreshCw className={auth.isFetching ? 'spin' : ''} aria-hidden="true" />
            {auth.isFetching ? t('正在重试') : t('重新验证')}
          </Button>
        )}
      />
    )
  }
  if (auth.data?.enabled && !auth.data.authenticated) {
    return <Navigate to="/login" replace />
  }
  return (
    <SearchSessionProvider>
      <Outlet />
    </SearchSessionProvider>
  )
}

export class AppErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null }

  static getDerivedStateFromError(error: Error) {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    void error
    void info
    void api.reportClientEvent('frontend_render_failure').catch(() => undefined)
  }

  reset = () => {
    this.setState({ error: null })
  }

  reload = () => {
    const url = new URL(window.location.href)
    url.searchParams.set('_recover', Date.now().toString(36))
    window.location.replace(url.toString())
  }

  render() {
    if (this.state.error) {
      return (
        <main className="fatal-error">
          <EmptyState
            role="alert"
            title={t('界面加载失败')}
            description={t('页面遇到无法恢复的渲染错误，未保存的界面修改可能需要重新输入。')}
            action={(
              <div className="empty-state-actions">
                <Button type="button" onClick={this.reset}>
                  <RefreshCw aria-hidden="true" />
                  {t('重试界面')}
                </Button>
                <Button type="button" variant="ghost" onClick={this.reload}>
                  {t('重新加载应用')}
                </Button>
              </div>
            )}
          />
        </main>
      )
    }
    return this.props.children
  }
}

export default function App() {
  return (
    <AppErrorBoundary>
      <Routes>
        <Route path="/login" element={lazyRoute(<LoginPage />)} />
        <Route element={<AuthGate />}>
          <Route element={<AppShell />}>
            <Route index element={<Navigate to="/search" replace />} />
            <Route path="/search" element={lazyRoute(<SearchPage />)} />
            <Route path="/results" element={lazyRoute(<SearchPage />)} />
            <Route path="/works/:workId" element={lazyRoute(<DetailPage />)} />
            <Route path="/search-history" element={lazyRoute(<SearchHistoryPage />)} />
            <Route path="/rankings" element={lazyRoute(<RankingsPage />)} />
            <Route path="/downloads" element={lazyRoute(<DownloadsPage />)} />
            <Route path="/library" element={lazyRoute(<LibraryPage />)} />
            <Route path="/metadata" element={lazyRoute(<MetadataPage />)} />
            <Route path="/subtitles" element={lazyRoute(<SubtitlesPage />)} />
            <Route path="/organizer" element={lazyRoute(<OrganizerPage />)} />
            <Route path="/history" element={<Navigate to="/settings#maintenance" replace />} />
            <Route path="/sites" element={lazyRoute(<SitesPage />)} />
            <Route path="/workflow-defaults" element={lazyRoute(<WorkflowDefaultsPage />)} />
            <Route path="/settings" element={lazyRoute(<SettingsPage />)} />
          </Route>
        </Route>
        <Route path="*" element={<Navigate to="/search" replace />} />
      </Routes>
    </AppErrorBoundary>
  )
}
