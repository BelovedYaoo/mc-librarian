import type { PaletteOptions, ThemeOptions } from '@mui/material/styles';

import { createTheme as createMuiTheme } from '@mui/material/styles';

// ----------------------------------------------------------------------

/**
 * 语义色：六个色各五档 + `contrastText`。
 *
 * 数值照抄 minimal-vite-ts 的 `src/theme/theme-config.ts`（不自行调色）：`lighter`/`darker`
 * 是 Minimal 在 MUI 的 light/main/dark 之外加的两档，供 soft / 极浅底色使用。
 */
const semanticColors = {
  primary: {
    lighter: '#C8FAD6',
    light: '#5BE49B',
    main: '#00A76F',
    dark: '#007867',
    darker: '#004B50',
    contrastText: '#FFFFFF',
  },
  secondary: {
    lighter: '#EFD6FF',
    light: '#C684FF',
    main: '#8E33FF',
    dark: '#5119B7',
    darker: '#27097A',
    contrastText: '#FFFFFF',
  },
  info: {
    lighter: '#CAFDF5',
    light: '#61F3F3',
    main: '#00B8D9',
    dark: '#006C9C',
    darker: '#003768',
    contrastText: '#FFFFFF',
  },
  success: {
    lighter: '#D3FCD2',
    light: '#77ED8B',
    main: '#22C55E',
    dark: '#118D57',
    darker: '#065E49',
    contrastText: '#ffffff',
  },
  warning: {
    lighter: '#FFF5CC',
    light: '#FFD666',
    main: '#FFAB00',
    dark: '#B76E00',
    darker: '#7A4100',
    contrastText: '#1C252E',
  },
  error: {
    lighter: '#FFE9D5',
    light: '#FFAC82',
    main: '#FF5630',
    dark: '#B71D18',
    darker: '#7A0916',
    contrastText: '#FFFFFF',
  },
} satisfies PaletteOptions;

/** grey 刻度 50–900，照抄 minimal-vite-ts 的 `theme-config.ts`。 */
const grey = {
  50: '#FCFDFD',
  100: '#F9FAFB',
  200: '#F4F6F8',
  300: '#DFE3E8',
  400: '#C4CDD5',
  500: '#919EAB',
  600: '#637381',
  700: '#454F5B',
  800: '#1C252E',
  900: '#141A21',
};

/**
 * `action` 的 alpha 值取自 minimal-vite-ts 的 `core/palette.ts`：底色都是 `grey[500]`
 * （`#919EAB`），Minimal 用 `varAlpha` 生成，这里直接写成等价的 `rgba()`。
 */
const grey500 = '145, 158, 171';

/** 浅色（light-only，本项目不做暗色模式与主题切换）调色板，照抄 Minimal 的 light 定义。 */
const lightPalette: PaletteOptions = {
  mode: 'light',
  ...semanticColors,
  grey,
  common: { black: '#000000', white: '#FFFFFF' },
  // 分隔线：Minimal 用 grey[500] 20%，比 MUI 默认的 rgba(0,0,0,0.12) 更柔和。
  divider: `rgba(${grey500}, 0.2)`,
  text: {
    primary: grey[800],
    secondary: grey[600],
    disabled: grey[500],
  },
  // `default` 是页面外壳（body / LayoutRoot），`paper` 是内容表面（顶栏、消息区、卡片），
  // `neutral` 是「凹陷」表面（导航列、代码块、表格斑马行）——Minimal 里这三者用途不同。
  background: {
    paper: '#FFFFFF',
    default: '#FFFFFF',
    neutral: grey[200],
  },
  action: {
    active: grey[600],
    hover: `rgba(${grey500}, 0.08)`,
    selected: `rgba(${grey500}, 0.16)`,
    focus: `rgba(${grey500}, 0.24)`,
    disabled: `rgba(${grey500}, 0.8)`,
    disabledBackground: `rgba(${grey500}, 0.24)`,
    hoverOpacity: 0.08,
    selectedOpacity: 0.08,
    focusOpacity: 0.12,
    activatedOpacity: 0.12,
    disabledOpacity: 0.48,
  },
};

const baseOptions: ThemeOptions = {
  palette: lightPalette,
  shape: { borderRadius: 8 },
  typography: {
    // `@fontsource-variable/public-sans` 注册的族名是 `Public Sans Variable`（引入见 src/global.css）。
    // 中文回退到 `Noto Sans SC`（系统字体，不打包 CJK 字体）。
    fontFamily: [
      '"Public Sans Variable"',
      '"Noto Sans SC"',
      'system-ui',
      '-apple-system',
      '"Segoe UI"',
      'sans-serif',
    ].join(','),
    h4: { fontWeight: 700 },
    h6: { fontWeight: 600 },
  },
};

export function createTheme(options: ThemeOptions = {}) {
  return createMuiTheme({
    ...baseOptions,
    ...options,
    palette: { ...baseOptions.palette, ...options.palette },
  });
}
