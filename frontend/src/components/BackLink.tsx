import { ArrowLeft } from 'lucide-react'
import { useNavigate } from 'react-router-dom'
import { t } from '../lib/i18n'

/** POP preserves the original route key, scroll position, filters and focus. */
export function BackLink({ fallback = '/search' }: { fallback?: string }) {
  const navigate = useNavigate()
  return <div className="page-back"><button type="button" className="button button-ghost" onClick={() => {
    if (Number(window.history.state?.idx) > 0) navigate(-1)
    else navigate(fallback, { replace: true })
  }}><ArrowLeft aria-hidden="true" />{t('返回')}</button></div>
}
