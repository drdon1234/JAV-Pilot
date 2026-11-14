/** Only application-relative destinations may survive a sign-in redirect. */
export function loginDestination(value: string | null): string {
  if (!value || !value.startsWith('/') || value.startsWith('//') || /[\\\u0000-\u0020]/.test(value)) return '/search'
  try {
    const parsed = new URL(value, 'http://app.invalid')
    if (parsed.origin !== 'http://app.invalid' || parsed.pathname === '/login') return '/search'
    return `${parsed.pathname}${parsed.search}${parsed.hash}`
  } catch { return '/search' }
}

export function loginUrl(destination: string): string {
  return `/login?${new URLSearchParams({ next: loginDestination(destination) })}`
}
