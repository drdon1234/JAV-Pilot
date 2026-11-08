import { NavLink } from 'react-router-dom'
import { t } from '../lib/i18n'

export function HistoryTabs() {
  return <nav className="history-tabs" aria-label={t('历史记录')}>
    <NavLink end to="/history">{t('播放记录')}</NavLink>
    <NavLink to="/search-history">{t('搜索记录')}</NavLink>
  </nav>
}
