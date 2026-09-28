import { Languages } from 'lucide-react'

import { browserPreferredLocale, changeLocale, LOCALES, localePreference, t, type LocalePreference } from '../lib/i18n'

/**
 * Interface language picker. "Follow browser" is the default and tracks the
 * browser (and so usually the system) language; choosing a language reloads
 * the page in it.
 */
export function LanguageSelect() {
  const detected = LOCALES.find((option) => option.code === browserPreferredLocale())?.label ?? ''
  return (
    <label className="language-select" title={t('界面语言')}>
      <Languages aria-hidden="true" />
      <select
        value={localePreference()}
        aria-label={t('界面语言')}
        onChange={(event) => changeLocale(event.target.value as LocalePreference)}
      >
        <option value="auto">{t('跟随浏览器（{language}）', { language: detected })}</option>
        {LOCALES.map((option) => (
          <option key={option.code} value={option.code} lang={option.code}>{option.label}</option>
        ))}
      </select>
    </label>
  )
}
