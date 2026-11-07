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

/** `common.black` 的通道值：暗色卡片阴影用它，深底上灰白阴影会发灰。 */
const blackChannel = '0, 0, 0';

/**
 * 「凹陷」表面（`background.neutral`）的底色。
 *
 * Minimal 这里用的是 `grey[200]`（`#F4F6F8`）——一个色相约 210° 的冷灰。本项目的主色是
 * `#00A76F`（色相约 160°），两者相差 50°，冷灰底与绿色主色并排显得脏。改用与主色同系
 * （色相约 152°）的淡绿，亮度与 `grey[200]` 几乎一致，所以原有对比度不受影响。
 */
const NEUTRAL_BACKGROUND_LIGHT = '#F1F7F4';

/**
 * 暗色的「凹陷」表面（代码块、行内代码、表格表头）。
 *
 * 取值来自用户提供的 IntelliJ 配色 `Bemylover.icls`（Atom One Dark 系）：比 paper 更深一档，
 * 让代码块看起来是「刻」在卡片里的。代码正文 `#ABB2BF` 在它上面 8.48:1。
 */
const NEUTRAL_BACKGROUND_DARK = '#15161A';

/**
 * 卡片阴影：照抄 minimal-vite-ts 的 `core/custom-shadows.ts` 里 `customShadows.card`
 * （`0 0 2px 0` + `0 12px 24px -4px`，都是低透明度）。
 *
 * Minimal 的卡片就靠这一层阴影在白底上「浮」起来，本身没有边框；这里额外补一条 `divider`
 * 细边框，让卡片在同样纯白的页面底上也有一圈看得见的边界。
 *
 * 取色按模式走：浅色用 `grey[500]`（现状），暗色改用 `common.black`——Minimal 的
 * `createCustomShadows(common.blackChannel)` 也是这么做的，灰白阴影在深底上会发灰。
 */
function cardShadow(colorChannel: string): string {
  return `0 0 2px 0 rgba(${colorChannel}, 0.2), 0 12px 24px -4px rgba(${colorChannel}, 0.12)`;
}

/** 明暗两态（`createTheme` 的入参、`ThemeProvider` 持有的状态）。 */
export type ThemeMode = 'light' | 'dark';

/**
 * `action` 的 alpha 与模式无关，只有 `active` 变：Minimal 的 `action('light' | 'dark')`
 * 也是 `grey[600]` / `grey[500]` 两档。
 */
function actionPalette(mode: ThemeMode) {
  return {
    active: mode === 'dark' ? grey[500] : grey[600],
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
  };
}

/** 浅色调色板，照抄 Minimal 的 light 定义。 */
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
    neutral: NEUTRAL_BACKGROUND_LIGHT,
  },
  action: actionPalette('light'),
};

/**
 * 暗色调色板：`background` / `action` 照抄 Minimal 的 dark 定义（`core/palette.ts`），语义色六色
 * 五档不动。两处偏离：`background.neutral`（见 `NEUTRAL_BACKGROUND_DARK`）与 `text.secondary`
 * （见下）。
 *
 * `text.secondary`：Minimal 的 dark 用 `grey[500]`（`#919EAB`），在 `paper`（`#1C252E`）上是
 * 5.68:1，数字上过 AA，但深色底上的浅灰字看起来比同比值的浅色底更暗（Helmholtz–Kohlrausch
 * 效应），实测 12px 的时间 / 用量 / 统计 / 辅助说明这类小字已接近读不清。改用 `grey[400]`
 * （`#C4CDD5`）→ 9.63:1。`text.primary`（`#FFFFFF`，15.52:1）与 `text.disabled`（禁用态，
 * 不受对比度要求约束）不动；light 的 `text.secondary`（`grey[600]`）也不动。
 */
const darkPalette: PaletteOptions = {
  mode: 'dark',
  ...semanticColors,
  grey,
  common: { black: '#000000', white: '#FFFFFF' },
  // 分隔线取方案里的「缩进参考线」色 `#3B4048`（比光标行 `#2C313C` 再亮一档）：
  // 深色下页面底与卡片的明度差只有 1.13:1，层次主要靠这条边框撑起来，太暗会糊成一片。
  divider: '#3B4048',
  text: {
    // 以下三色与代码高亮取自 `Bemylover.icls`（Atom One Dark 系）：
    // 正文 `#ABB2BF`（paper 上 7.22:1）、次要 `#95A1B2`（5.88:1）、禁用 `#5C6370`（禁用态不要求 AA）。
    // 次要色比方案里的行注释色 `#7F8B9D` 提亮了两档：深底上的浅灰小字观感比同比值的浅底更暗
    // （Helmholtz–Kohlrausch 效应），4.46:1 的注释色放在 12px 的时间/用量上会读不清。
    primary: '#ABB2BF',
    secondary: '#95A1B2',
    disabled: '#5C6370',
  },
  background: {
    // page 用编辑器底色、paper 提亮一档做卡片、neutral 再压深一档给代码块。
    paper: '#21252B',
    default: '#191A1C',
    neutral: NEUTRAL_BACKGROUND_DARK,
  },
  // 交互态用方案正文色的低透明度叠加（与 Minimal 的 `grey[500]` 叠加同一做法，只换基色）。
  action: {
    active: '#ABB2BF',
    hover: 'rgba(171, 178, 191, 0.08)',
    selected: 'rgba(171, 178, 191, 0.16)',
    focus: 'rgba(171, 178, 191, 0.24)',
    disabled: 'rgba(171, 178, 191, 0.8)',
    disabledBackground: 'rgba(171, 178, 191, 0.24)',
    hoverOpacity: 0.08,
    selectedOpacity: 0.08,
    focusOpacity: 0.12,
    activatedOpacity: 0.12,
    disabledOpacity: 0.48,
  },
};

const baseOptions: ThemeOptions = {
  shape: { borderRadius: 8 },
  components: {
    // 滚动条：系统默认的滚动条在暗色下是一条亮白的宽条，和深底格格不入（截图里最扎眼的
    // 就是它）。统一定成细滚动条，颜色随模式走；`border` + `backgroundClip: content-box`
    // 让滑块比轨道窄一圈，视觉上是「内嵌」而不是「贴着边缘」。
    //
    // 同时写 `scrollbarWidth`/`scrollbarColor`（Firefox）与 `::-webkit-scrollbar`（Chrome /
    // Edge / Safari）：目前只有这两套，没有第三套标准。
    MuiCssBaseline: {
      styleOverrides: (theme) => {
        const dark = theme.palette.mode === 'dark';
        // 暗底用白色低透明度、亮底用 grey[500]，各自在对面底色上都够看得清。
        const thumb = dark ? 'rgba(255, 255, 255, 0.18)' : `rgba(${grey500}, 0.32)`;
        const thumbHover = dark ? 'rgba(255, 255, 255, 0.3)' : `rgba(${grey500}, 0.5)`;

        return {
          '*': { scrollbarWidth: 'thin', scrollbarColor: `${thumb} transparent` },
          '*::-webkit-scrollbar': { width: 10, height: 10 },
          '*::-webkit-scrollbar-track': { backgroundColor: 'transparent' },
          '*::-webkit-scrollbar-thumb': {
            backgroundColor: thumb,
            borderRadius: 8,
            // 透明边框 + content-box 裁切：滑块实际 6px，两侧各留 2px 空隙。
            border: '2px solid transparent',
            backgroundClip: 'content-box',
          },
          '*::-webkit-scrollbar-thumb:hover': { backgroundColor: thumbHover },
          '*::-webkit-scrollbar-corner': { backgroundColor: 'transparent' },
        };
      },
    },
    // 卡片：细边框 + 极淡阴影。边框用 `divider`（grey[500] 20%），阴影用 Minimal 的卡片
    // token（按模式取色），圆角按 Minimal 的 `Number(shape.borderRadius) * 2`（8 → 16px）。
    MuiCard: {
      styleOverrides: {
        root: ({ theme }) => ({
          border: `1px solid ${theme.palette.divider}`,
          borderRadius: Number(theme.shape.borderRadius) * 2,
          boxShadow: cardShadow(theme.palette.mode === 'dark' ? blackChannel : grey500),
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

/** 按模式建主题；`ThemeProvider` 切换模式时重建。 */
export function createTheme(mode: ThemeMode = 'light') {
  return createMuiTheme({
    ...baseOptions,
    palette: mode === 'dark' ? darkPalette : lightPalette,
  });
}
