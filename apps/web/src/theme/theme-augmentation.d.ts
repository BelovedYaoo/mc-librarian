/**
 * MUI 调色板的类型扩展。
 *
 * MUI 自带的口径里，语义色只有 `light` / `main` / `dark` / `contrastText`，背景只有
 * `default` / `paper`。本项目照抄 minimal-vite-ts 的调色板，多出两档语义色（`lighter` /
 * `darker`）和一层背景（`neutral`），需要在这里声明，否则 `create-theme.ts` 里写这些键
 * 会报「对象字面量只能指定已知属性」。
 *
 * 这个文件必须保持是「模块」（有顶层 import/export），否则 `declare module` 会被当成
 * 声明一个新的环境模块，而不是扩展已有的 `@mui/material/styles`。
 */
import type {} from '@mui/material/styles';

declare module '@mui/material/styles' {
  interface PaletteColor {
    lighter: string;
    darker: string;
  }

  interface SimplePaletteColorOptions {
    lighter?: string;
    darker?: string;
  }

  interface TypeBackground {
    /** 「凹陷」表面：导航列、代码块、表格斑马行。 */
    neutral: string;
  }
}
