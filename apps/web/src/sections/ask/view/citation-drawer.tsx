import type { Citation, FileSlice } from 'src/lib/api-client';

import { useEffect, useMemo, useRef, useState } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Drawer from '@mui/material/Drawer';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';

import { ApiError, readFile } from 'src/lib/api-client';

// ----------------------------------------------------------------------

/** 代码行的固定高度：虚拟化按它算窗口与滚动定位，必须与 `lineHeight` 一致。 */
const LINE_HEIGHT = 20;
/** 可视区上下各多渲染的行数，滚动时留出缓冲，避免白屏闪烁。 */
const OVERSCAN = 200;

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
}: {
  slice: FileSlice;
  highlight: readonly number[];
  focusLine: number;
}) {
  const lines = useMemo(() => (slice.content === '' ? [] : slice.content.split('\n')), [slice]);
  const startLine = slice.start;

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
              bgcolor: marked ? 'warning.light' : 'transparent',
            }}
          >
            <Box
              component="span"
              sx={{
                width: 56,
                flexShrink: 0,
                pr: 1.5,
                textAlign: 'right',
                color: 'text.disabled',
                userSelect: 'none',
              }}
            >
              {lineNo}
            </Box>

            <Box component="span" sx={{ flex: '1 1 auto', whiteSpace: 'pre' }}>
              {line === '' ? ' ' : line}
            </Box>
          </Box>
        );
      })}

      <Box sx={{ height: Math.max(0, (total - to) * LINE_HEIGHT) }} />
    </Box>
  );
}
