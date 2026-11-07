import type { Theme } from '@mui/material/styles';

import { alpha } from '@mui/material/styles';
// ----------------------------------------------------------------------

/**
 * `AnswerMarkdown` 的 markdown 容器样式（`<Box sx={markdownContainerSx}>`）。
 *
 * 布局与字号无关；代码 token 的颜色由 Shiki 主题（`shiki-highlighter.ts`）提供。其余元素（标题、
 * 表格边框与表头底色、行内代码底、代码块底、引用块）都走调色板——`divider` / `background.neutral`
 * / `text.secondary`——暗色下由 dark 调色板自动接管。
 *
 * 表格需要横向滚动容器；其余元素靠后代选择器统一上样式。行内代码与代码块的区分交给 CSS
 * （`& pre code` 覆盖 `& code`），不靠 JS 猜父节点。
 */
export function markdownContainerSx(theme: Theme) {
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
    // 代码块的外壳（底色/边框/header/限高/行号）由 `MarkdownCode` 提供，这里只把 `pre` 自身的
    // 间距与底色清掉——否则 `& pre` 的后代选择器（特异性更高）会盖掉组件上的 `sx`。
    '& pre': {
      m: 0,
      p: 0,
      bgcolor: 'transparent',
      borderRadius: 0,
      overflowX: 'visible',
    },
    // diff 行的底色与文字色。行结构（行号列）由 `MarkdownCode` 自己排，这里只负责配色。
    '& .code-line.diff-add': {
      bgcolor: alpha(theme.palette.success.main, 0.16),
      color: theme.palette.mode === 'dark' ? '#89CA78' : '#22863A',
    },
    '& .code-line.diff-del': {
      bgcolor: alpha(theme.palette.error.main, 0.16),
      color: theme.palette.mode === 'dark' ? '#FF9AA2' : '#B31D28',
    },
    // hunk 头（`@@ -48,6 +_,7 @@`）压弱，避免和正文挤成一片。
    '& .code-line.diff-hunk': { color: 'text.secondary' },
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
