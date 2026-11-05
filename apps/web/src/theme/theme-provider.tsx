import type { ReactNode } from 'react';

import { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import CssBaseline from '@mui/material/CssBaseline';
import { ThemeProvider as MuiThemeProvider } from '@mui/material/styles';

import type { ThemeMode } from './create-theme';

import { createTheme } from './create-theme';

// ----------------------------------------------------------------------

export type ThemeProviderProps = {
  children: ReactNode;
};

/** 用户**显式**选过的模式存这里；没有这个键时跟随系统。 */
const STORAGE_KEY = 'mc-librarian-theme-mode';

const DARK_QUERY = '(prefers-color-scheme: dark)';

type ThemeModeContextValue = {
  mode: ThemeMode;
  toggleMode: () => void;
};

const ThemeModeContext = createContext<ThemeModeContextValue | null>(null);

/** 已保存的显式选择；没选过（或值非法）返回 `null`。 */
function storedMode(): ThemeMode | null {
  const value = window.localStorage.getItem(STORAGE_KEY);

  return value === 'light' || value === 'dark' ? value : null;
}

function systemMode(): ThemeMode {
  return window.matchMedia(DARK_QUERY).matches ? 'dark' : 'light';
}

/**
 * 主题外壳：持有明暗模式并据此建主题。
 *
 * 首屏模式：`localStorage` 里有显式选择就用它，否则看 `prefers-color-scheme`。切换时写回
 * `localStorage`。系统主题变化只在用户没做过显式选择时才跟随——点过按钮之后就以按钮为准。
 */
export function ThemeProvider({ children }: ThemeProviderProps) {
  const [mode, setMode] = useState<ThemeMode>(() => storedMode() ?? systemMode());

  const theme = useMemo(() => createTheme(mode), [mode]);

  useEffect(() => {
    const media = window.matchMedia(DARK_QUERY);

    const handleChange = (event: MediaQueryListEvent) => {
      if (storedMode() === null) setMode(event.matches ? 'dark' : 'light');
    };

    media.addEventListener('change', handleChange);

    return () => media.removeEventListener('change', handleChange);
  }, []);

  const toggleMode = useCallback(() => {
    setMode((current) => {
      const next: ThemeMode = current === 'dark' ? 'light' : 'dark';
      window.localStorage.setItem(STORAGE_KEY, next);

      return next;
    });
  }, []);

  const value = useMemo(() => ({ mode, toggleMode }), [mode, toggleMode]);

  return (
    <ThemeModeContext.Provider value={value}>
      <MuiThemeProvider theme={theme}>
        <CssBaseline />
        {children}
      </MuiThemeProvider>
    </ThemeModeContext.Provider>
  );
}

/** 顶栏的主题切换按钮用；在 `ThemeProvider` 之外调用是编程错误，直接抛。 */
export function useThemeMode(): ThemeModeContextValue {
  const value = useContext(ThemeModeContext);

  if (value === null) throw new Error('useThemeMode 必须在 ThemeProvider 内使用');

  return value;
}
