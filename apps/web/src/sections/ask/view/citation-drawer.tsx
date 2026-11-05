import type { Citation, FileSlice } from 'src/lib/api-client';

import { useEffect, useMemo, useRef, useState } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Drawer from '@mui/material/Drawer';
import Stack from '@mui/material/Stack';
import Switch from '@mui/material/Switch';
import Typography from '@mui/material/Typography';
import { useTheme } from '@mui/material/styles';

import { ApiError, readFile } from 'src/lib/api-client';

import type { HighlightLanguage, HighlightedLine, HighlightTheme } from './shiki-highlighter';

import { highlightLines, languageForPath, themeForMode } from './shiki-highlighter';

// ----------------------------------------------------------------------

/** 代码行的固定高度：虚拟化按它算窗口与滚动定位，必须与 `lineHeight` 一致。 */
const LINE_HEIGHT = 20;
/** 可视区上下各多渲染的行数，滚动时留出缓冲，避免白屏闪烁。 */
const OVERSCAN = 200;
/** 高亮前的防抖窗口：连续滚动时不为每一帧都跑一次 tokenizer。 */
const HIGHLIGHT_DEBOUNCE_MS = 80;

/**
 * 引用区间的底色（暗色）。浅色用 `warning.light`（`#FFD666`）——亮黄底配 github-light 的深色 token。
 * 暗色下那个亮黄底配 Shiki github-dark 的浅色 token 只有 ~1.1:1，读不出来；`warning.dark`
 * （`#B76E00`）也只有 ~3.1:1。改用低饱和暗绿 `#1C3A30`：色相 160°，与主色 `#00A76F` 同系，
 * 在冷灰蓝底 + 绿色主色的页面上大面积着色不冲突（此前用的 `#33290D` 是色相 44° 的琥珀 / 橄榄褐，
 * 与页面色系相差 ~116°）。正文 token `#E1E4E8` → 9.70:1、关键字 `#F97583` → 4.65:1、
 * 行号（`text.secondary` `#C4CDD5`）→ 7.68:1。
 *
 * 注释 token 不在这里处理：Shiki 官方主题的 `#6A737D` 配任何底色都上不了 AA（在本底色上只有
 * 2.57:1），已在 `shiki-highlighter.ts` 里按模式换成调色板值（light `#454F5B` / dark `#919EAB`）。
 */
const HIGHLIGHT_DARK = '#1C3A30';

type LoadState =
  | { status: 'idle' }
  | { status: 'loading' }
  | { status: 'error'; error: string }
  | { status: 'ready'; slice: FileSlice; total: number };

/**
 * 引用抽屉：右侧打开，展示**整个文件**并高亮 `citation.lines`。
 *
 * 整文件读取要两次 `/v1/read`：契约要求 `end <= total_lines`，所以先用引用那几行探出
 * `total_lines`，再读 `1..total_lines`。超过 token 预算时后端返回 `truncated`，这里明确提示，
 * 不静默截断。大文件靠 `FileLines` 只渲染可视区 ±200 行。
 */
export function CitationDrawer({
  open,
  citation,
  version,
  onClose,
}: {
  open: boolean;
  citation: Citation | null;
  version: string;
  onClose: () => void;
}) {
  const [state, setState] = useState<LoadState>({ status: 'idle' });
  const [copied, setCopied] = useState(false);
  // 自动换行：抽屉很高，横向滚动条会落在最底部够不到（实测长行被右缘截断）。
  const [wrap, setWrap] = useState(true);

  useEffect(() => {
    if (!open || citation === null) return undefined;

    let cancelled = false;
    const { path } = citation;
    const [start, end] = citation.lines;

    setState({ status: 'loading' });

    void (async () => {
      try {
        const probe = await readFile({ version, path, start, end });
        const total = probe.total_lines ?? probe.end;
        const slice = await readFile({ version, path, start: 1, end: total, max_tokens: 32000 });

        if (!cancelled) setState({ status: 'ready', slice, total });
      } catch (cause) {
        if (!cancelled) {
          setState({
            status: 'error',
            error: cause instanceof ApiError ? cause.message : String(cause),
          });
        }
      }
    })();

    return () => {
      cancelled = true;
    };
  }, [open, citation, version]);

  useEffect(() => {
    if (!open) setCopied(false);
  }, [open]);

  const handleCopy = async () => {
    if (citation === null) return;

    try {
      await navigator.clipboard.writeText(citation.path);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <Drawer
      anchor="right"
      open={open}
      onClose={onClose}
      slotProps={{
        paper: { sx: { width: { xs: '100%', md: 'min(920px, 78vw)' }, maxWidth: '100%' } },
      }}
    >
      {citation !== null && (
        <Stack sx={{ height: '100%', minHeight: 0 }}>
          <Box
            sx={{
              flexShrink: 0,
              px: 2,
              py: 1.5,
              borderBottom: '1px solid',
              borderColor: 'divider',
            }}
          >
            <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
              <Typography
                variant="subtitle1"
                sx={{ flex: '1 1 auto', fontFamily: 'monospace', wordBreak: 'break-all' }}
              >
                {citation.path}
              </Typography>

              <Button size="small" onClick={() => void handleCopy()}>
                {copied ? '已复制' : '复制路径'}
              </Button>

              <Button size="small" onClick={onClose}>
                关闭
              </Button>
            </Stack>

            <Stack
              direction="row"
              spacing={1}
              sx={{ mt: 0.5, alignItems: 'center', flexWrap: 'wrap' }}
            >
              {citation.symbol && <Chip size="small" variant="outlined" label={citation.symbol} />}

              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                第 {citation.lines.join('-')} 行
              </Typography>

              {state.status === 'ready' && (
                <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                  共 {state.total} 行
                </Typography>
              )}

              <Stack direction="row" spacing={0.5} sx={{ alignItems: 'center', ml: 'auto' }}>
                <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                  自动换行
                </Typography>
                <Switch size="small" checked={wrap} onChange={(e) => setWrap(e.target.checked)} />
              </Stack>
            </Stack>
          </Box>

          <Box sx={{ flex: '1 1 auto', minHeight: 0 }}>
            {state.status === 'loading' && (
              <Stack sx={{ height: '100%', alignItems: 'center', justifyContent: 'center' }}>
                <CircularProgress size={28} />
              </Stack>
            )}

            {state.status === 'error' && (
              <Box sx={{ p: 2 }}>
                <Alert severity="error">读取源码失败：{state.error}</Alert>
              </Box>
            )}

            {state.status === 'ready' && (
              <Stack sx={{ height: '100%', minHeight: 0 }}>
                {state.slice.truncated && (
                  <Alert severity="warning" sx={{ borderRadius: 0 }}>
                    文件过大，仅显示前 {state.slice.end} 行（共 {state.total} 行）。
                  </Alert>
                )}

                <FileViewer
                  wrap={wrap}
                  slice={state.slice}
                  highlight={citation.lines}
                  focusLine={citation.lines[0]}
                />
              </Stack>
            )}
          </Box>
        </Stack>
      )}
    </Drawer>
  );
}

// ----------------------------------------------------------------------

/**
 * 整个文件的分行视图：按滚动位置只渲染可视区 ±200 行（几千行的文件不会生成几千个 DOM 节点）。
 * 行号固定宽度，高亮行用明显底色；打开时把 `focusLine` 滚到「居中偏上」。
 */
function FileViewer({
  slice,
  highlight,
  focusLine,
  wrap,
}: {
  slice: FileSlice;
  highlight: readonly number[];
  focusLine: number;
  wrap: boolean;
}) {
  const lines = useMemo(() => (slice.content === '' ? [] : slice.content.split('\n')), [slice]);
  const startLine = slice.start;

  const { palette } = useTheme();
  // 引用区间底色按模式取：浅色亮黄、暗色低饱和暗黄（见 `HIGHLIGHT_DARK`）。
  const highlightBg = palette.mode === 'dark' ? HIGHLIGHT_DARK : palette.warning.light;
  const scrollRef = useRef<HTMLDivElement | null>(null);
  const frame = useRef<number | null>(null);
  const [view, setView] = useState({ top: 0, height: 0 });

  const total = lines.length;

  const measure = () => {
    const element = scrollRef.current;

    if (element === null) return;

    setView((current) =>
      current.top === element.scrollTop && current.height === element.clientHeight
        ? current
        : { top: element.scrollTop, height: element.clientHeight }
    );
  };

  const handleScroll = () => {
    if (frame.current !== null) return;

    frame.current = requestAnimationFrame(() => {
      frame.current = null;
      measure();
    });
  };

  useEffect(() => {
    const element = scrollRef.current;

    if (element === null) return undefined;

    const observer = new ResizeObserver(measure);
    observer.observe(element);

    return () => {
      observer.disconnect();

      if (frame.current !== null) cancelAnimationFrame(frame.current);
    };
  }, []);

  useEffect(() => {
    const element = scrollRef.current;

    if (element === null) return;

    // 打开时滚到第一处高亮：留 35% 视口高度在上方，做到「居中偏上」。
    const offset = (focusLine - startLine) * LINE_HEIGHT - element.clientHeight * 0.35;
    element.scrollTop = Math.max(0, offset);
    measure();
  }, [focusLine, startLine, lines]);

  const firstVisible = Math.max(0, Math.floor(view.top / LINE_HEIGHT) - OVERSCAN);
  const from = Math.min(firstVisible, Math.max(0, total - 1));
  const to = Math.max(
    Math.min(total, Math.ceil((view.top + view.height) / LINE_HEIGHT) + OVERSCAN),
    Math.min(total, from + 1)
  );

  // 只把**可视窗口**那段交给 Shiki：整文件 `codeToHtml` 会在每次滚动时全量重排。
  const code = lines.slice(from, to).join('\n');
  const highlighted = useHighlightedLines(
    code,
    languageForPath(slice.path),
    themeForMode(palette.mode)
  );

  return (
    <Box
      ref={scrollRef}
      onScroll={handleScroll}
      sx={{
        flex: '1 1 auto',
        minHeight: 0,
        overflow: 'auto',
        bgcolor: 'action.hover',
        fontFamily: 'monospace',
        fontSize: 13,
      }}
    >
      <Box sx={{ height: from * LINE_HEIGHT }} />

      {lines.slice(from, to).map((line, index) => {
        const lineNo = startLine + from + index;
        const marked = lineNo >= highlight[0] && lineNo <= highlight[1];

        return (
          <Box
            key={lineNo}
            sx={{
              display: 'flex',
              lineHeight: `${LINE_HEIGHT}px`,
              bgcolor: marked ? highlightBg : 'transparent',
            }}
          >
            <Box
              component="span"
              sx={{
                width: 56,
                flexShrink: 0,
                pr: 1.5,
                textAlign: 'right',
                // 用 `text.secondary` 而不是 `text.disabled`：灰色 disabled 在高亮行
                // （`warning.light`）上只有 1.96:1，行号会消失。暗色下 `text.secondary` 是
                // `grey[400]`（`#C4CDD5`），在高亮行 `#1C3A30` 上 7.68:1、在普通行底
                // （`action.hover` 叠 `paper`）上 8.45:1，仍明显暗于代码正文（`#E1E4E8` 的
                // 9.70:1），「比正文淡」的行号观感还在，所以不另给行号单独取色。
                color: 'text.secondary',
                userSelect: 'none',
              }}
            >
              {lineNo}
            </Box>

            <Box
              component="span"
              sx={{
                flex: '1 1 auto',
                // 默认自动换行：抽屉很高，横向滚动条会落在最底部、实际够不到（实测截断在右缘）。
                // 关掉换行时用 pre + 父级 overflow-x 兜底，保持列对齐。
                whiteSpace: wrap ? 'pre-wrap' : 'pre',
                overflowWrap: wrap ? 'anywhere' : 'normal',
                pr: 1,
              }}
            >
              <CodeText line={line} tokens={highlighted?.[index] ?? null} />
            </Box>
          </Box>
        );
      })}

      <Box sx={{ height: Math.max(0, (total - to) * LINE_HEIGHT) }} />
    </Box>
  );
}

// ----------------------------------------------------------------------

/**
 * 可视窗口的异步高亮。
 *
 * 返回的 token 只在 `code` 与当前窗口一致时才有效，否则回退纯文本（首帧）——窗口一变就
 * 立即失效，避免把上一段的着色套到新的一段上。防抖 `HIGHLIGHT_DEBOUNCE_MS` 保证连续滚动
 * 不会为每一帧都跑一次 tokenizer。着色只改前景色、不改字符与字重，因此换行与行高都不变，
 * 高亮落地时不会跳位。
 */
function useHighlightedLines(
  code: string,
  lang: HighlightLanguage | 'text',
  theme: HighlightTheme
): HighlightedLine[] | null {
  const [state, setState] = useState<{ code: string; lines: HighlightedLine[] } | null>(null);

  useEffect(() => {
    if (lang === 'text') return undefined;

    let cancelled = false;
    const timer = setTimeout(() => {
      highlightLines(code, lang, theme)
        .then((lines) => {
          if (!cancelled) setState({ code, lines });
        })
        .catch(() => {
          // 高亮失败退回纯文本即可，不影响阅读，也不必打断用户。
        });
    }, HIGHLIGHT_DEBOUNCE_MS);

    return () => {
      cancelled = true;
      clearTimeout(timer);
    };
  }, [code, lang, theme]);

  return state !== null && state.code === code ? state.lines : null;
}

/**
 * 一行的内容：高亮未就绪时是纯文本，就绪后是逐段着色的 `span`。
 *
 * 只写 `color`（不写字重/斜体）：字符与字宽与纯文本完全一致，高亮替换时不会重新换行，
 * 因此 `LINE_HEIGHT` 恒定、滚动不跳位。
 */
function CodeText({ line, tokens }: { line: string; tokens: HighlightedLine | null }) {
  if (tokens === null || tokens.length === 0) return line === '' ? ' ' : line;

  return tokens.map((token) => (
    <span key={token.offset} style={{ color: token.color }}>
      {token.content}
    </span>
  ));
}
