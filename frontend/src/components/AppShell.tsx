import { useQuery } from '@tanstack/react-query'
import { Clapperboard, Download, FileImage, FolderCog, Globe2, History, Library, ListRestart, Search, Settings, SlidersHorizontal, Trophy } from 'lucide-react'
import { type RefObject, useEffect, useLayoutEffect, useRef, useState } from 'react'
import { Link, NavLink, Outlet, useLocation, useNavigationType } from 'react-router-dom'

import { api } from '../lib/api'
import { MAIN_CONTENT_SCROLL_QUERY, usesMainContentScroll } from '../lib/pageScroll'
import { ThemeButton } from './ThemeProvider'
import { StatusBadge } from './ui'

const MAX_SCROLL_POSITIONS = 24

interface RouteAnnouncement {
  key: string
  title: string
}

function subscribeToMediaQuery(query: MediaQueryList | undefined, listener: (event: MediaQueryListEvent) => void): () => void {
  if (!query) return () => undefined
  if (typeof query.addEventListener === 'function') {
    query.addEventListener('change', listener)
    return () => query.removeEventListener('change', listener)
  }
  query.addListener(listener)
  return () => query.removeListener(listener)
}

function readScrollTop(mainContent: HTMLElement | null): number {
  return usesMainContentScroll() && mainContent ? mainContent.scrollTop : window.scrollY
}

function setWindowScrollTop(top: number): void {
  window.scrollTo({ top, left: 0, behavior: 'instant' } as ScrollToOptions)
}

function setScrollTop(mainContent: HTMLElement | null, top: number): void {
  if (usesMainContentScroll() && mainContent) {
    mainContent.scrollTop = top
    return
  }
  setWindowScrollTop(top)
}

function rememberScrollPosition(positions: Map<string, number>, key: string, top: number): void {
  positions.delete(key)
  positions.set(key, top)
  while (positions.size > MAX_SCROLL_POSITIONS) {
    const oldestKey = positions.keys().next().value
    if (typeof oldestKey !== 'string') return
    positions.delete(oldestKey)
  }
}

function rememberFocusReturnKey(keys: Map<string, string>, locationKey: string, focusReturnKey: string): void {
  keys.delete(locationKey)
  keys.set(locationKey, focusReturnKey)
  while (keys.size > MAX_SCROLL_POSITIONS) {
    const oldestKey = keys.keys().next().value
    if (typeof oldestKey !== 'string') return
    keys.delete(oldestKey)
  }
}

function focusReturnKeyFromTarget(target: EventTarget | null): string {
  if (!(target instanceof Element)) return ''
  return target.closest<HTMLElement>('[data-focus-return-key]')?.dataset.focusReturnKey?.trim() ?? ''
}

function findFocusReturnTarget(mainContent: HTMLElement, key: string): HTMLElement | null {
  return Array.from(mainContent.querySelectorAll<HTMLElement>('[data-focus-return-key]'))
    .find((candidate) => candidate.dataset.focusReturnKey === key) ?? null
}

function useRouteLifecycle(mainContentRef: RefObject<HTMLElement | null>): RouteAnnouncement | null {
  const location = useLocation()
  const navigationType = useNavigationType()
  const positionsRef = useRef(new Map<string, number>())
  const focusReturnKeysRef = useRef(new Map<string, string>())
  const previousPathnameRef = useRef(location.pathname)
  const [announcement, setAnnouncement] = useState<RouteAnnouncement | null>(null)

  useEffect(() => {
    if (!('scrollRestoration' in window.history)) return
    const previous = window.history.scrollRestoration
    window.history.scrollRestoration = 'manual'
    return () => {
      window.history.scrollRestoration = previous
    }
  }, [])

  useLayoutEffect(() => {
    const locationKey = location.key
    const mainContent = mainContentRef.current
    const rememberCurrentPosition = () => {
      rememberScrollPosition(positionsRef.current, locationKey, readScrollTop(mainContent))
    }
    window.addEventListener('scroll', rememberCurrentPosition, { passive: true })
    mainContent?.addEventListener('scroll', rememberCurrentPosition, { passive: true })
    return () => {
      window.removeEventListener('scroll', rememberCurrentPosition)
      mainContent?.removeEventListener('scroll', rememberCurrentPosition)
    }
  }, [location.key, mainContentRef])

  useLayoutEffect(() => {
    const restoredTop = navigationType === 'POP' ? positionsRef.current.get(location.key) : undefined
    const targetTop = restoredTop ?? 0
    const mainContent = mainContentRef.current
    setScrollTop(mainContent, targetTop)
    if (targetTop <= 0 || Math.abs(readScrollTop(mainContent) - targetTop) <= 1 || !mainContent) return

    let timeoutId: number | null = null
    let retryId: number | null = null
    let retryUsesAnimationFrame = false
    let stopped = false
    const stopRestoring = () => {
      if (stopped) return
      stopped = true
      observer.disconnect()
      if (timeoutId !== null) window.clearTimeout(timeoutId)
      if (retryId !== null) {
        if (retryUsesAnimationFrame) window.cancelAnimationFrame(retryId)
        else window.clearTimeout(retryId)
      }
      mainContent.removeEventListener('wheel', stopRestoring, true)
      mainContent.removeEventListener('touchstart', stopRestoring, true)
      mainContent.removeEventListener('pointerdown', stopRestoring, true)
      mainContent.removeEventListener('keydown', stopRestoring, true)
    }
    const tryRestoring = () => {
      if (stopped) return
      setScrollTop(mainContent, targetTop)
      if (Math.abs(readScrollTop(mainContent) - targetTop) <= 1) stopRestoring()
    }
    const scheduleRestoreAfterLayout = () => {
      if (stopped || retryId !== null) return
      const retry = () => {
        retryId = null
        tryRestoring()
      }
      if (typeof window.requestAnimationFrame === 'function') {
        retryUsesAnimationFrame = true
        retryId = window.requestAnimationFrame(retry)
      } else {
        retryUsesAnimationFrame = false
        retryId = window.setTimeout(retry, 0)
      }
    }
    const observer = new MutationObserver(() => {
      tryRestoring()
      scheduleRestoreAfterLayout()
    })
    observer.observe(mainContent, { childList: true, subtree: true })
    mainContent.addEventListener('wheel', stopRestoring, { capture: true, passive: true })
    mainContent.addEventListener('touchstart', stopRestoring, { capture: true, passive: true })
    mainContent.addEventListener('pointerdown', stopRestoring, true)
    mainContent.addEventListener('keydown', stopRestoring, true)
    scheduleRestoreAfterLayout()
    timeoutId = window.setTimeout(stopRestoring, 10_000)
    return stopRestoring
  }, [location.key, mainContentRef, navigationType])

  useEffect(() => {
    const mainContent = mainContentRef.current
    if (!mainContent) return
    const rememberTrigger = (event: Event) => {
      const focusReturnKey = focusReturnKeyFromTarget(event.target)
      if (focusReturnKey) rememberFocusReturnKey(focusReturnKeysRef.current, location.key, focusReturnKey)
    }
    mainContent.addEventListener('click', rememberTrigger, true)
    return () => mainContent.removeEventListener('click', rememberTrigger, true)
  }, [location.key, mainContentRef])

  useEffect(() => {
    const pathChanged = previousPathnameRef.current !== location.pathname
    previousPathnameRef.current = location.pathname
    if (!pathChanged || !mainContentRef.current) return

    const returnKey = navigationType === 'POP' ? focusReturnKeysRef.current.get(location.key) : undefined
    let announced = false
    let focused = false
    const applyRouteFocus = () => {
      const heading = mainContentRef.current?.querySelector<HTMLElement>('[data-page-heading]')
      if (heading && !announced) {
        const title = heading.textContent?.trim()
        if (title) setAnnouncement({ key: location.key, title })
        announced = true
      }
      if (!focused && navigationType === 'POP' && returnKey && mainContentRef.current) {
        const target = findFocusReturnTarget(mainContentRef.current, returnKey)
        if (target) {
          target.focus({ preventScroll: true })
          focused = true
        }
      } else if (!focused && navigationType !== 'POP' && heading) {
        heading.focus({ preventScroll: true })
        focused = true
      }
      return announced && (focused || (navigationType === 'POP' && !returnKey))
    }

    if (applyRouteFocus()) return
    const observer = new MutationObserver(() => {
      if (applyRouteFocus()) observer.disconnect()
    })
    observer.observe(mainContentRef.current, { childList: true, subtree: true })
    return () => observer.disconnect()
  }, [location.key, location.pathname, mainContentRef, navigationType])

  return announcement
}

const navigationGroups = [
  {
    label: '媒体工作流',
    items: [
      { to: '/search', label: '搜索', icon: Search },
      { to: '/search-history', label: '搜索记录', icon: ListRestart },
      { to: '/rankings', label: '排行榜', icon: Trophy },
      { to: '/downloads', label: '下载', icon: Download },
      { to: '/library', label: '媒体库', icon: Library },
      { to: '/metadata', label: '元数据', icon: FileImage },
      { to: '/organizer', label: '整理', icon: FolderCog },
    ],
  },
  {
    label: '系统管理',
    items: [
      { to: '/history', label: '历史', icon: History },
      { to: '/sites', label: '站点', icon: Globe2 },
      { to: '/workflow-defaults', label: '默认参数', icon: SlidersHorizontal },
      { to: '/settings', label: '设置', icon: Settings },
    ],
  },
]

function Navigation({ searchTarget, searchActive }: { searchTarget: string; searchActive: boolean }) {
  return (
    <nav className="primary-nav" aria-label="主导航">
      {navigationGroups.map((group) => (
        <div className="nav-group" key={group.label}>
          <span className="nav-section-label">{group.label}</span>
          {group.items.map(({ to, label, icon: Icon }) => {
            const isSearch = to === '/search'
            if (isSearch) {
              return (
                <Link
                  key={to}
                  to={searchTarget}
                  aria-current={searchActive ? 'page' : undefined}
                  className={searchActive ? 'nav-link active' : 'nav-link'}
                >
                  <Icon aria-hidden="true" />
                  <span>{label}</span>
                </Link>
              )
            }
            return (
              <NavLink
                key={to}
                to={to}
                className={({ isActive }) => (isActive ? 'nav-link active' : 'nav-link')}
              >
                <Icon aria-hidden="true" />
                <span>{label}</span>
              </NavLink>
            )
          })}
        </div>
      ))}
    </nav>
  )
}

export function AppShell() {
  const location = useLocation()
  const mainContentRef = useRef<HTMLElement | null>(null)
  const latestResultsUrlRef = useRef('/search')
  const currentUrl = `${location.pathname}${location.search}${location.hash}`
  useLayoutEffect(() => {
    const roots = [document.documentElement, document.body, document.getElementById('root')]
      .filter((element): element is HTMLElement => element instanceof HTMLElement)
    const mobileQuery = globalThis.matchMedia?.(MAIN_CONTENT_SCROLL_QUERY)
    if (mobileQuery?.matches) setWindowScrollTop(0)
    roots.forEach((element) => element.classList.add('app-shell-active'))

    const handleBreakpointChange = (event: MediaQueryListEvent) => {
      const mainContent = mainContentRef.current
      if (event.matches) {
        const previousTop = window.scrollY
        setWindowScrollTop(0)
        if (mainContent && previousTop > 0) mainContent.scrollTop = previousTop
        return
      }
      if (!mainContent) return
      const previousTop = mainContent.scrollTop
      mainContent.scrollTop = 0
      setWindowScrollTop(previousTop)
    }
    const unsubscribeBreakpoint = subscribeToMediaQuery(mobileQuery, handleBreakpointChange)
    return () => {
      unsubscribeBreakpoint()
      roots.forEach((element) => element.classList.remove('app-shell-active'))
    }
  }, [mainContentRef])
  useEffect(() => {
    if (location.pathname === '/results') latestResultsUrlRef.current = currentUrl
  }, [currentUrl, location.pathname])
  const searchTarget = location.pathname === '/search'
    ? '/search'
    : location.pathname === '/results'
      ? currentUrl
      : latestResultsUrlRef.current
  const searchActive = location.pathname === '/search'
    || location.pathname === '/results'
    || location.pathname.startsWith('/works/')
  const routeAnnouncement = useRouteLifecycle(mainContentRef)
  const status = useQuery({
    queryKey: ['downloader-status'],
    queryFn: api.downloaderStatus,
    refetchInterval: 30_000,
  })
  const online = Boolean(status.data?.ok)
  const configured = Boolean(status.data?.configured)
  const serviceLabel = status.isPending
    ? '检测中'
    : status.isError
      ? '状态未知'
      : online
        ? `qB ${status.data?.version || '在线'}`
        : configured
          ? '连接异常'
          : '未配置'

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            <Clapperboard />
          </div>
          <div>
            <strong>JAV Pilot</strong>
            <span>媒体管理工作台</span>
          </div>
        </div>
        <Navigation searchTarget={searchTarget} searchActive={searchActive} />
        <div className="sidebar-footer">
          <div className="sidebar-service">
            <span>下载服务</span>
            <StatusBadge tone={online ? 'success' : status.isError || configured ? 'warning' : 'neutral'}>
              <span className="semantic-dot" aria-hidden="true" />
              {serviceLabel}
            </StatusBadge>
          </div>
          <ThemeButton />
        </div>
      </aside>

      <header className="mobile-header">
        <div className="brand compact">
          <div className="brand-mark" aria-hidden="true">
            <Clapperboard />
          </div>
          <strong>JAV Pilot</strong>
        </div>
        <ThemeButton />
      </header>

      <main className="main-content" ref={mainContentRef}>
        <Outlet />
      </main>

      <div className="route-announcement" role="status" aria-label="页面已切换" aria-live="polite" aria-atomic="true">
        {routeAnnouncement ? <span key={routeAnnouncement.key}>{routeAnnouncement.title}</span> : null}
      </div>

      <div className="mobile-nav-wrap">
        <Navigation searchTarget={searchTarget} searchActive={searchActive} />
      </div>
    </div>
  )
}
