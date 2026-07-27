export const THEME_STORAGE_KEY = 'ohmystock.theme'

export const themeModes = ['system', 'light', 'dark'] as const

export type ThemeMode = (typeof themeModes)[number]
export type ResolvedTheme = Exclude<ThemeMode, 'system'>

export const ohMyStockPreset = Object.freeze({
  colorSchemeQuery: '(prefers-color-scheme: dark)',
  storageKey: THEME_STORAGE_KEY,
  modes: themeModes
})

