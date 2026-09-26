export const MAIN_CONTENT_SCROLL_QUERY = '(max-width: 900px)'

export function usesMainContentScroll(): boolean {
  return globalThis.matchMedia?.(MAIN_CONTENT_SCROLL_QUERY)?.matches ?? false
}

export function scrollPageToTop(): void {
  const options: ScrollToOptions = { top: 0, left: 0, behavior: 'smooth' }
  if (usesMainContentScroll()) {
    document.querySelector<HTMLElement>('.main-content')?.scrollTo(options)
    return
  }
  window.scrollTo(options)
}
