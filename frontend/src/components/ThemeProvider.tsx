import { Moon, Sun } from 'lucide-react'
import { createContext, type ReactNode, useContext, useLayoutEffect, useMemo, useState } from 'react'

type ThemeMode = 'light' | 'dark'

const ThemeContext = createContext<{ mode: ThemeMode; setMode: (mode: ThemeMode) => void } | null>(null)
const DARK_THEME_COLOR = '#10141b'
const LIGHT_THEME_COLOR = '#f5f7fa'

function storedTheme(): ThemeMode {
  try {
    const value = window.localStorage.getItem('jav-pilot-theme')
    if (value === 'light' || value === 'dark') return value
    return window.matchMedia('(prefers-color-scheme: dark)').matches ? 'dark' : 'light'
  } catch {
    return 'light'
  }
}

export function ThemeProvider({ children }: { children: ReactNode }) {
  const [mode, setMode] = useState<ThemeMode>(storedTheme)

  useLayoutEffect(() => {
    const root = document.documentElement
    const syncThemeColor = () => {
      const color = mode === 'dark' ? DARK_THEME_COLOR : LIGHT_THEME_COLOR
      document.querySelector('meta[name="theme-color"]')?.setAttribute('content', color)
    }

    root.dataset.themeChanging = 'true'
    root.dataset.theme = mode
    root.style.colorScheme = mode
    syncThemeColor()
    try {
      window.localStorage.setItem('jav-pilot-theme', mode)
    } catch {
      // Restricted mobile storage must not prevent the selected theme from rendering.
    }

    let secondFrame = 0
    const firstFrame = window.requestAnimationFrame(() => {
      secondFrame = window.requestAnimationFrame(() => {
        delete root.dataset.themeChanging
      })
    })
    return () => {
      window.cancelAnimationFrame(firstFrame)
      if (secondFrame) window.cancelAnimationFrame(secondFrame)
      delete root.dataset.themeChanging
    }
  }, [mode])

  const value = useMemo(() => ({ mode, setMode }), [mode])
  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>
}

export function ThemeButton() {
  const context = useContext(ThemeContext)
  if (!context) return null
  const options = [
    { mode: 'light' as const, label: '亮色', icon: Sun },
    { mode: 'dark' as const, label: '暗色', icon: Moon },
  ]
  return (
    <div className="theme-switcher" role="group" aria-label="界面主题">
      {options.map(({ mode, label, icon: Icon }) => (
        <button
          type="button"
          className={`theme-option ${context.mode === mode ? 'active' : ''}`}
          aria-label={`${label}主题`}
          aria-pressed={context.mode === mode}
          title={`${label}主题`}
          onClick={() => context.setMode(mode)}
          key={mode}
        >
          <Icon aria-hidden="true" />
          <span>{label}</span>
        </button>
      ))}
    </div>
  )
}
