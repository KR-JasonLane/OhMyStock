import { useCallback, useEffect, useMemo, useState } from 'react'

import {
  ohMyStockPreset,
  type ResolvedTheme,
  type ThemeMode
} from './preset'
import { ThemeContext } from './context'

function isThemeMode(value: string | null): value is ThemeMode {
  return value !== null && ohMyStockPreset.modes.some((mode) => mode === value)
}

function storedMode(): ThemeMode {
  if (typeof window === 'undefined') return 'system'
  try {
    const value = window.localStorage.getItem(ohMyStockPreset.storageKey)
    return isThemeMode(value) ? value : 'system'
  } catch {
    return 'system'
  }
}

function systemTheme(): ResolvedTheme {
  if (typeof window === 'undefined' || typeof window.matchMedia !== 'function') return 'light'
  return window.matchMedia(ohMyStockPreset.colorSchemeQuery).matches ? 'dark' : 'light'
}

export function ThemeProvider({ children }: React.PropsWithChildren): React.JSX.Element {
  const [mode, setModeState] = useState<ThemeMode>(storedMode)
  const [system, setSystem] = useState<ResolvedTheme>(systemTheme)
  const resolvedTheme = mode === 'system' ? system : mode

  useEffect(() => {
    document.documentElement.dataset.theme = resolvedTheme
    document.documentElement.style.colorScheme = resolvedTheme
  }, [resolvedTheme])

  useEffect(() => {
    if (typeof window.matchMedia !== 'function') return
    const mediaQuery = window.matchMedia(ohMyStockPreset.colorSchemeQuery)
    const update = (event: MediaQueryListEvent): void => setSystem(event.matches ? 'dark' : 'light')
    mediaQuery.addEventListener('change', update)
    return () => mediaQuery.removeEventListener('change', update)
  }, [])

  const setMode = useCallback((nextMode: ThemeMode): void => {
    setModeState(nextMode)
    try {
      window.localStorage.setItem(ohMyStockPreset.storageKey, nextMode)
    } catch {
      // 저장소 접근이 차단되어도 현재 세션의 테마 변경은 유지한다.
    }
  }, [])

  const value = useMemo(
    () => ({ mode, resolvedTheme, setMode }),
    [mode, resolvedTheme, setMode]
  )

  return <ThemeContext value={value}>{children}</ThemeContext>
}
