import type { Theme } from '@mui/material/styles';

import { alpha } from '@mui/material/styles';
// ----------------------------------------------------------------------

/**
 * 代码高亮的 token 色：`github` / `github-dark` 两套官方主题的色值（照抄，不自调）。
 *
 * 这里不再 `import 'highlight.js/styles/github.css'`——那套 CSS 只适配浅色；同时引
 * `github-dark.css` 又会因为两套 `.hljs-*` 选择器互相覆盖（后引入的赢）而无法按模式切换。
 * 改成按 `palette.mode` 取下面这张表，写进 markdown 容器的 styleOverrides。
 */
const codeTokenColors = {
  light: {
    /** 代码正文。 */
    text: '#24292E',
    keyword: '#D73A49',
    string: '#032F62',
    /**
     * github 的官方注释色是 `#6A737D`，但那是配它自己的底色（`#FFFFFF`）的；本项目的代码块底是
     * `background.neutral`（`#F1F7F4`），`#6A737D` 在它上面只有 4.44:1，低于 AA 的 4.5。
     * 改用 `grey[700]`（`#454F5B`）→ 7.67:1。与 dark 侧同一处理（官方色配的不是我们的底色）。
     */
    comment: '#454F5B',
    number: '#005CC5',
    title: '#6F42C1',
    builtIn: '#E36209',
    // diff：新增/删除行的文字色（底色由 `.code-line.diff-*` 给），hunk 头（`@@`）压弱。
    inserted: '#22863A',
    deleted: '#B31D28',
    meta: '#6A737D',
  },
  dark: {
    // 取自 `Bemylover.icls`（Atom One Dark 系）的 Java 属性色，底为 `background.neutral`（#15161A）：
    // 正文 #ABB2BF 8.48:1、关键字 #749DED 6.70:1、字符串 #89CA78 9.28:1、
    // 数字/参数 #D19A66 7.34:1、类名 #E5C07B 10.47:1、函数调用 #61AFEF 7.65:1。
    text: '#ABB2BF',
    keyword: '#749DED',
    string: '#89CA78',
    // 方案里的行注释色 #7F8B9D（5.24:1，过 AA）；块注释 #5C6370 只有 2.99:1，不用于代码正文。
    comment: '#7F8B9D',
    number: '#D19A66',
    title: '#E5C07B',
    builtIn: '#61AFEF',
    // 取自 Bemylover.icls：字符串绿 `#89CA78`；hunk 头用注释色。
    // 删除行用 `#E06C75` 的提亮版 `#FF9AA2`：原色在 16% 红底上只有 4.68:1（刚过 AA，
    // 观感发闷），提亮后 7.4:1——同一色相，只是加亮，不引入新色。
    inserted: '#89CA78',
    deleted: '#FF9AA2',
    meta: '#7F8B9D',
  },
} as const;

/**
 * `AnswerMarkdown` 的 markdown 容器样式（`<Box sx={markdownContainerSx}>`）。
 *
 * 布局、字号与模式无关；只有代码 token 的颜色按 `palette.mode` 取两套值。其余元素（标题、
 * 表格边框与表头底色、行内代码底、代码块底、引用块）都走调色板——`divider` / `background.neutral`
 * / `text.secondary`——暗色下由 dark 调色板自动接管。
 *
 * 表格需要横向滚动容器；其余元素靠后代选择器统一上样式。行内代码与代码块的区分交给 CSS
 * （`& pre code` 覆盖 `& code`），不靠 JS 猜父节点。
 */
export function markdownContainerSx(theme: Theme) {
  const tokens = codeTokenColors[theme.palette.mode];

  return {
    // 用 `:first-of-type` 而不是 `:first-child`：后者会触发 emotion 的 SSR 不安全告警（console.error）。
    '& > :first-of-type': { mt: 0 },
    '& > :last-child': { mb: 0 },
    '& h1, & h2, & h3, & h4, & h5, & h6': { mt: 2, mb: 1, fontWeight: 700, lineHeight: 1.3 },
    '& h1': { fontSize: '1.5rem' },
    '& h2': { fontSize: '1.3rem' },
    '& h3': { fontSize: '1.15rem' },
    '& h4, & h5, & h6': { fontSize: '1rem' },
    // 段落与列表项限宽提升可读性；表格/代码块**不限**（答案里的宽表与长代码行靠可用宽度）。
    '& p': { my: 1, lineHeight: 1.7, maxWidth: 900 },
    '& ul, & ol': { my: 1, pl: 3 },
    '& li': { my: 0.5, lineHeight: 1.7, maxWidth: 900 },
    '& li > p': { my: 0 },
    '& blockquote': {
      my: 1,
      pl: 2,
      borderLeft: '3px solid',
      borderColor: 'divider',
      color: 'text.secondary',
    },
    '& a': { color: 'primary.main' },
    '& hr': { my: 2, border: 0, borderTop: '1px solid', borderColor: 'divider' },
    '& del': { color: 'text.secondary' },
    // 行内代码。底色用主色（`#00A76F`）8% 的淡绿，与「凹陷」表面同一色系：
    // `action.hover`（grey 8%）偏蓝，在白色消息区上既弱又和绿色主色不搭。
    '& code': {
      fontFamily: 'monospace',
      fontSize: '0.875em',
      px: 0.5,
      py: 0.25,
      borderRadius: 0.5,
      bgcolor: 'rgba(0, 167, 111, 0.08)',
    },
    // 代码块的外壳（底色/边框/header/限高）由 `CodeBlockPre` 提供，这里只把 `pre` 自身的中
    // 间距与底色清掉——否则 `& pre` 的后代选择器（特异性更高）会盖掉组件上的 `sx`。
    '& pre': {
      m: 0,
      p: 0,
      bgcolor: 'transparent',
      borderRadius: 0,
      overflowX: 'visible',
    },
    '& .code-line': {
      display: 'block',
      position: 'relative',
      counterIncrement: 'code-line',
      minHeight: '1.6em',
      pl: '3.8em',
      pr: 2,
      // 行尾多余空白不撑出横向滚动；长行本身仍按 `pre` 横向滚。
      '&::before': {
        content: 'counter(code-line)',
        position: 'absolute',
        left: 0,
        width: '2.8em',
        textAlign: 'right',
        color: 'text.secondary',
        opacity: 0.6,
        userSelect: 'none',
      },
    },
    '& .code-line.diff-add': {
      bgcolor: alpha(theme.palette.success.main, 0.16),
    },
    '& .code-line.diff-del': {
      bgcolor: alpha(theme.palette.error.main, 0.16),
    },
    // hunk 头（`@@ -48,6 +_,7 @@`）：highlight.js 的 diff 语法不包 meta，按行首加类。
    '& .code-line.diff-hunk': { color: 'text.secondary' },
    // highlight.js 的 diff 语法把 `+`/`-` 行标成 addition/deletion；
    // 不映射的话它们会落回正文色，整块看着是一团灰。
    '& .hljs-addition': { color: tokens.inserted },
    '& .hljs-deletion': { color: tokens.deleted },
    '& .hljs-meta': { color: tokens.meta },
    // 代码正文与 token 色。`rehype-highlight` 把 `.hljs` 加在 `<code>` 上、`.hljs-*` 加在 token 的
    // `<span>` 上，所以在这里统一覆盖；不引任何 highlight.js 主题 CSS。
    '& pre code': {
      display: 'block',
      bgcolor: 'transparent',
      p: 0,
      // 一行一个 `span.code-line`（rehype 阶段切好），行号用 CSS counter——不占复制内容。
      counterReset: 'code-line',
      fontSize: '0.8125rem',
      lineHeight: 1.6,
      whiteSpace: 'pre',
      overflowX: 'visible',
      color: tokens.text,
    },
    '& .hljs-keyword': { color: tokens.keyword },
    '& .hljs-string': { color: tokens.string },
    '& .hljs-comment': { color: tokens.comment },
    '& .hljs-number, & .hljs-literal': { color: tokens.number },
    '& .hljs-title, & .hljs-function': { color: tokens.title },
    '& .hljs-built_in, & .hljs-type': { color: tokens.builtIn },
    '& table': { borderCollapse: 'collapse', fontSize: '0.875rem' },
    '& th, & td': {
      border: '1px solid',
      borderColor: 'divider',
      px: 1.5,
      py: 0.75,
      textAlign: 'left',
    },
    // 表头用「凹陷」表面：Minimal 的 `MuiTableCell.head` 也是 `background.neutral`。
    '& thead th': { bgcolor: 'background.neutral', fontWeight: 600 },
  };
}
