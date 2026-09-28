/**
 * Interface language. Simplified Chinese source strings double as message
 * keys: code passes the Chinese text (with {name} placeholders) to t(), and
 * every other language ships a catalog in src/locales/<code>.ts that maps the
 * Chinese text to its translation.
 * Missing entries fall back to Chinese, and `tools/i18n_catalog.py` checks
 * the catalogs against the strings used in the code.
 *
 * The catalog is loaded with a top-level await, so every module that
 * imports t() is evaluated after it and module-level labels are translated
 * too. Changing the language reloads the page for the same reason.
 */

export const LOCALES = [
  { code: 'zh-CN', label: '简体中文' },
  { code: 'zh-TW', label: '繁體中文' },
  { code: 'en', label: 'English' },
  { code: 'ja', label: '日本語' },
  { code: 'fr', label: 'Français' },
  { code: 'es', label: 'Español' },
  { code: 'ru', label: 'Русский' },
  { code: 'ar', label: 'العربية' },
] as const

export type Locale = typeof LOCALES[number]['code']

export const SOURCE_LOCALE: Locale = 'zh-CN'
const FALLBACK_LOCALE: Locale = 'en'
const STORAGE_KEY = 'jav-pilot-locale'
const RTL_LOCALES: ReadonlySet<Locale> = new Set(['ar'])
const catalogs = import.meta.glob<Record<string, string>>('../locales/*.ts', { import: 'default' })

type MessageValue = string | number | boolean | null | undefined

function isLocale(value: unknown): value is Locale {
  return LOCALES.some((locale) => locale.code === value)
}

/** The closest supported language for a BCP 47 tag, or null. */
export function matchLocale(tag: string): Locale | null {
  const value = tag.trim().toLowerCase()
  if (!value) return null
  if (value === 'zh' || value.startsWith('zh-')) {
    return /-(tw|hk|mo|hant)\b/.test(value) ? 'zh-TW' : 'zh-CN'
  }
  const base = value.split('-')[0]
  return LOCALES.find((locale) => locale.code === base)?.code ?? null
}

function storedLocale(): Locale | null {
  try {
    const value = globalThis.localStorage?.getItem(STORAGE_KEY)
    return isLocale(value) ? value : null
  } catch {
    return null
  }
}

function browserLocale(): Locale {
  const tags = globalThis.navigator?.languages?.length
    ? globalThis.navigator.languages
    : [globalThis.navigator?.language ?? '']
  for (const tag of tags) {
    const match = matchLocale(tag)
    if (match) return match
  }
  return FALLBACK_LOCALE
}

export type LocalePreference = Locale | 'auto'

const preference: LocalePreference = storedLocale() ?? 'auto'
let locale: Locale = preference === 'auto' ? browserLocale() : preference
let messages: Record<string, string> = {}

if (locale !== SOURCE_LOCALE) {
  const load = catalogs[`../locales/${locale}.ts`]
  try {
    messages = load ? await load() : {}
  } catch {
    // A catalog that fails to load leaves the interface in Chinese rather
    // than blank; the next page load tries again.
    messages = {}
  }
}

if (globalThis.document) {
  document.documentElement.lang = locale
  document.documentElement.dir = RTL_LOCALES.has(locale) ? 'rtl' : 'ltr'
}

// Following the browser (and through it the system) language: when that
// language changes while the page is open, reload into the new one.
if (preference === 'auto') {
  globalThis.addEventListener?.('languagechange', () => {
    if (browserLocale() !== locale) globalThis.location?.reload()
  })
}

/** The interface language. */
export function currentLocale(): Locale {
  return locale
}

/**
 * The language that title and synopsis translations are requested in: the
 * interface language, so translations read in the language the user chose.
 */
export function translationTarget(): Locale {
  return locale
}

/** The chosen language, or 'auto' when the interface follows the browser. */
export function localePreference(): LocalePreference {
  return preference
}

/** The supported language closest to the browser and system language. */
export function browserPreferredLocale(): Locale {
  return browserLocale()
}

/**
 * Stores the choice ('auto' follows the browser language) and reloads so
 * every label is rendered in it. Without storage the page keeps following
 * the browser language.
 */
export function changeLocale(next: LocalePreference): void {
  if (next === preference) return
  try {
    if (next === 'auto') globalThis.localStorage?.removeItem(STORAGE_KEY)
    else globalThis.localStorage?.setItem(STORAGE_KEY, next)
  } catch {
    return
  }
  globalThis.location?.reload()
}

/**
 * Translates a Chinese source string. `{name}` placeholders are filled from
 * `params`; unknown placeholders are left as written.
 */
export function t(message: string, params?: Record<string, MessageValue>): string {
  return fill(messages[message] || message, params)
}

/**
 * Like t() for a Chinese text that needs a different translation in this
 * place, e.g. tc('按钮', '下载') for the verb where t('下载') is the page
 * name. Catalogs key it as "按钮|下载"; Chinese shows the text itself.
 */
export function tc(context: string, message: string, params?: Record<string, MessageValue>): string {
  return fill(messages[`${context}|${message}`] || message, params)
}

function fill(template: string, params?: Record<string, MessageValue>): string {
  if (!params) return template
  return template.replace(/\{(\w+)\}/g, (match, name: string) => (
    Object.prototype.hasOwnProperty.call(params, name) ? String(params[name] ?? '') : match
  ))
}

/** Translates a Chinese text received from the server when a catalog entry exists. */
export function translateKnown(message: string): string {
  return messages[message.trim()] || message
}
