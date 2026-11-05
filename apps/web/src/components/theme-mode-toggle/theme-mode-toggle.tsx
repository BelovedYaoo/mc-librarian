import type { SxProps, Theme } from '@mui/material/styles';

import IconButton from '@mui/material/IconButton';
import SvgIcon from '@mui/material/SvgIcon';
import Tooltip from '@mui/material/Tooltip';

import { useThemeMode } from 'src/theme';

// ----------------------------------------------------------------------

/**
 * 顶栏的主题切换按钮：浅色时显示月亮（点击切到深色），深色时显示太阳。
 *
 * 图标用内联 SVG——项目没有引入图标库，为一个按钮不值得加依赖。按钮的可见文案由
 * `aria-label` / tooltip 提供（`切换到深色` / `切换到浅色`）。
 */
export function ThemeModeToggle({ sx }: { sx?: SxProps<Theme> }) {
  const { mode, toggleMode } = useThemeMode();
  const isDark = mode === 'dark';
  const label = isDark ? '切换到浅色' : '切换到深色';

  return (
    <Tooltip title={label}>
      <IconButton size="small" color="inherit" aria-label={label} onClick={toggleMode} sx={sx}>
        <SvgIcon fontSize="small">{isDark ? <SunPath /> : <MoonPath />}</SvgIcon>
      </IconButton>
    </Tooltip>
  );
}

// ----------------------------------------------------------------------

/** 月亮（`DarkMode`）：浅色模式下显示。 */
function MoonPath() {
  return (
    <path d="M12 3c-4.97 0-9 4.03-9 9s4.03 9 9 9 9-4.03 9-9c0-.46-.04-.92-.1-1.36-.98 1.37-2.58 2.26-4.4 2.26-2.98 0-5.4-2.42-5.4-5.4 0-1.81.89-3.42 2.26-4.4-.44-.06-.9-.1-1.36-.1z" />
  );
}

/** 太阳（`LightMode`）：深色模式下显示。 */
function SunPath() {
  return (
    <path d="M12 7c-2.76 0-5 2.24-5 5s2.24 5 5 5 5-2.24 5-5-2.24-5-5-5zM2 13h2c.55 0 1-.45 1-1s-.45-1-1-1H2c-.55 0-1 .45-1 1s.45 1 1 1zm18 0h2c.55 0 1-.45 1-1s-.45-1-1-1h-2c-.55 0-1 .45-1 1s.45 1 1 1zM11 2v2c0 .55.45 1 1 1s1-.45 1-1V2c0-.55-.45-1-1-1s-1 .45-1 1zm0 18v2c0 .55.45 1 1 1s1-.45 1-1v-2c0-.55-.45-1-1-1s-1 .45-1 1zM5.99 4.58c-.39-.39-1.03-.39-1.41 0-.39.39-.39 1.03 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0s.39-1.03 0-1.41L5.99 4.58zm12.37 12.37c-.39-.39-1.03-.39-1.41 0-.39.39-.39 1.03 0 1.41l1.06 1.06c.39.39 1.03.39 1.41 0 .39-.39.39-1.03 0-1.41l-1.06-1.06zm1.06-10.96c.39-.39.39-1.03 0-1.41-.39-.39-1.03-.39-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06zM7.05 18.36c.39-.39.39-1.03 0-1.41-.39-.39-1.03-.39-1.41 0l-1.06 1.06c-.39.39-.39 1.03 0 1.41s1.03.39 1.41 0l1.06-1.06z" />
  );
}
