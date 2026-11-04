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

/**
 * 「凹陷」表面（`background.neutral`）的底色。
 *
 * Minimal 这里用的是 `grey[200]`（`#F4F6F8`）——一个色相约 210° 的冷灰。本项目的主色是
 * `#00A76F`（色相约 160°），两者相差 50°，冷灰底与绿色主色并排显得脏。改用与主色同系
 * （色相约 152°）的淡绿，亮度与 `grey[200]` 几乎一致，所以原有对比度不受影响。
 */
const NEUTRAL_BACKGROUND = '#F1F7F4';

/**
 * 卡片阴影：照抄 minimal-vite-ts 的 `core/custom-shadows.ts` 里 `customShadows.card`
 * （`0 0 2px 0` + `0 12px 24px -4px`，都是 `grey[500]` 的低透明度）。
 *
 * Minimal 的卡片就靠这一层阴影在白底上「浮」起来，本身没有边框；这里额外补一条 `divider`
 * 细边框，让卡片在同样纯白的页面底上也有一圈看得见的边界。
 */
const CARD_SHADOW = `0 0 2px 0 rgba(${grey500}, 0.2), 0 12px 24px -4px rgba(${grey500}, 0.12)`;

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
  // `default` 是页面外壳（body / LayoutRoot）与侧栏，`paper` 是内容表面（顶栏、消息区、卡片），
  // `neutral` 只用在需要「凹陷」感的地方（代码块、行内代码、表格表头）——**不做面板分区**。
  // 层次靠 `divider` 边框与卡片阴影，不靠灰底面板（Minimal 的侧栏就是白的）。
  background: {
    paper: '#FFFFFF',
    default: '#FFFFFF',
    neutral: NEUTRAL_BACKGROUND,
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
  components: {
    // 卡片：细边框 + 极淡阴影。边框用 `divider`（grey[500] 20%），阴影用 Minimal 的卡片
    // token，圆角按 Minimal 的 `Number(shape.borderRadius) * 2`（8 → 16px）。
    MuiCard: {
      styleOverrides: {
        root: ({ theme }) => ({
          border: `1px solid ${theme.palette.divider}`,
          borderRadius: Number(theme.shape.borderRadius) * 2,
          boxShadow: CARD_SHADOW,
        }),
      },
    },
  },
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
