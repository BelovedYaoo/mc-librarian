import type { Citation, FileSlice, Outline } from 'src/lib/api-client';

import { useEffect, useLayoutEffect, useMemo, useRef, useState } from 'react';
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

import { ApiError, getOutline, readFile } from 'src/lib/api-client';

import type { HighlightLanguage, HighlightedLine, HighlightTheme } from './shiki-highlighter';

import { highlightLines, languageForPath, themeForMode } from './shiki-highlighter';

// ----------------------------------------------------------------------

/** 代码行的固定高度：虚拟化按它算窗口与滚动定位，必须与 `lineHeight` 一致。 */
const LINE_HEIGHT = 20;
/** 换行符。`split('\n')` 里的转义在两处（源码、脚本）都容易出错，抽成常量更直白。 */
const NEWLINE = '\n';
/** 高亮前的防抖窗口：打开抽屉时内容会分几次到达（先取引用区间、再取整文件），合并成一次。 */
const HIGHLIGHT_DEBOUNCE_MS = 80;

/**
 * 引用区间的底色（暗色）。浅色用 `warning.light`（`#FFD666`）——亮黄底配 github-light 的深色 token。
 * 暗色下取 `Bemylover.icls` 的「光标行」色 `#2C313C`：与新增的深灰底（page `#191A1C` /
 * paper `#21252B`）同源，大面积着色不抢眼。实测正文 `#ABB2BF` 6.11:1、关键字 `#749DED` 4.83:1、
 * 行号（`text.secondary` `#8D99AB`）4.52:1，三项都过 AA。
 */
const HIGHLIGHT_DARK = '#2C313C';

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
  // 参数名层：可读（Parchment）/ 原始占位名（$$0、p_xxx_）。两棵树行号 1:1，切换不丢位置。
  const [names, setNames] = useState<'parchment' | 'original'>('parchment');
  // 只看引用：大文件里引用常常在第 900 行，全文铺开时找不到重点。
  const [focusOnly, setFocusOnly] = useState(false);
  // 结构信息（类/方法 + 精确行区间）：方法折叠、大纲侧栏、当前符号三处共用一次请求。
  const [outline, setOutline] = useState<Outline | null>(null);
  // 代码区顶部当前可见的行号：用于「当前符号」与大纲高亮。
  const [currentLine, setCurrentLine] = useState<number | null>(null);
  // 大纲点击跳转：带 nonce 是为了「再点同一项」也能触发。
  const [scrollTo, setScrollTo] = useState<{ line: number; nonce: number } | null>(null);

  useEffect(() => {
    if (!open || citation === null) return undefined;

    let cancelled = false;
    const { path } = citation;
    const [start, end] = citation.lines;

    setState({ status: 'loading' });
    setOutline(null);

    // 结构信息与正文并行取：拿不到就不折叠/不显示侧栏，不影响读代码。
    void getOutline({ version, path })
      .then((data) => {
        if (!cancelled) setOutline(data);
      })
      .catch(() => {
        if (!cancelled) setOutline(null);
      });

    void (async () => {
      try {
        const probe = await readFile({ version, path, start, end, names });
        const total = probe.total_lines ?? probe.end;
        const slice = await readFile({
          version,
          path,
          start: 1,
          end: total,
          max_tokens: 32000,
          names,
        });

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
  }, [open, citation, version, names]);

  useEffect(() => {
    if (!open) setCopied(false);
  }, [open]);

  // 顶部「当前符号」：由当前可见行 + outline 推出来（最内层方法优先）。
  const symbol = symbolAt(outline, currentLine ?? citation?.lines[0] ?? 1);

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
              {citation.symbol && (
                <Chip
                  size="small"
                  variant="outlined"
                  label={shortenSymbol(citation.symbol, citation.path)}
                />
              )}

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
                  只看引用
                </Typography>
                <Switch
                  size="small"
                  checked={focusOnly}
                  onChange={(e) => setFocusOnly(e.target.checked)}
                />
              </Stack>

              <Stack direction="row" spacing={0.5} sx={{ alignItems: 'center' }}>
                <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                  原始参数名
                </Typography>
                <Switch
                  size="small"
                  checked={names === 'original'}
                  onChange={(event) =>
                    setNames(event.target.checked ? 'original' : 'parchment')
                  }
                />
              </Stack>

              <Stack direction="row" spacing={0.5} sx={{ alignItems: 'center' }}>
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

                <Box sx={{ display: 'flex', flex: '1 1 auto', minHeight: 0 }}>
                  <Box
                    sx={{ display: 'flex', flexDirection: 'column', flex: '1 1 auto', minWidth: 0 }}
                  >
                    {symbol !== null && (
                      <Box
                        sx={{
                          flexShrink: 0,
                          px: 1.5,
                          py: 0.5,
                          fontSize: 12,
                          fontFamily: 'monospace',
                          color: 'text.secondary',
                          bgcolor: 'background.paper',
                          borderBottom: '1px solid',
                          borderColor: 'divider',
                        }}
                      >
                        {symbol}
                        {currentLine !== null && ` · 第 ${currentLine} 行`}
                      </Box>
                    )}

                    <FileViewer
                      wrap={wrap}
                      focusOnly={focusOnly}
                      slice={state.slice}
                      highlight={citation.lines}
                      focusLine={citation.lines[0]}
                      outline={outline}
                      scrollTo={scrollTo}
                      onVisibleLine={setCurrentLine}
                    />
                  </Box>

                  {outline !== null && outline.classes.length > 0 && (
                    <OutlinePanel
                      outline={outline}
                      currentLine={currentLine}
                      onJump={(line) => setScrollTo({ line, nonce: Date.now() })}
                    />
                  )}
                </Box>
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
type FoldRegion = {
  start: number;
  end: number;
  label: string;
  kind: 'import' | 'class' | 'method';
};

/**
 * Java 文件里可以折叠的区间。目前只做 **import 块**——它最长、最没信息量，也是 IDEA 默认折叠的。
 *
 * 识别方式：连续的 `import ...` 行（允许中间夹空行）。返回的 `start`/`end` 是**文件行号**
 * （与 `slice.start` 对齐），不是数组下标。
 */
function importRegions(lines: string[], startLine: number): FoldRegion[] {
  const regions: FoldRegion[] = [];
  let index = 0;

  while (index < lines.length) {
    if (!/^\s*import\s/.test(lines[index])) {
      index += 1;
      continue;
    }

    const first = index;
    let last = index;
    let cursor = index + 1;

    while (cursor < lines.length) {
      if (/^\s*import\s/.test(lines[cursor])) {
        last = cursor;
        cursor += 1;
        continue;
      }
      // 空行后面还跟着 import：仍算同一块（有些文件按包分组留空行）。
      if (
        /^\s*$/.test(lines[cursor]) &&
        cursor + 1 < lines.length &&
        /^\s*import\s/.test(lines[cursor + 1])
      ) {
        cursor += 1;
        continue;
      }
      break;
    }

    regions.push({
      start: startLine + first,
      end: startLine + last,
      label: 'import',
      kind: 'import',
    });
    index = last + 1;
  }

  return regions;
}

/** `net.minecraft.world.Foo.Bar` → `Bar`（大纲/标题里只显示短名）。 */
function simpleName(fqn: string): string {
  return fqn.slice(fqn.lastIndexOf('.') + 1);
}

/**
 * 全部可折叠区间：import 块 + 类 + 方法（后两者来自 `/v1/outline` 的精确行区间）。
 *
 * 方法只折「≥ 3 行」的：一行 getter 折起来没有收益，反而让 `▾` 满屏。
 */
function foldableRegions(
  lines: string[],
  startLine: number,
  outline: Outline | null
): FoldRegion[] {
  const regions = importRegions(lines, startLine);

  if (outline === null) return regions;

  for (const cls of outline.classes) {
    const classLines = cls.lines;

    if (classLines !== undefined) {
      regions.push({
        start: classLines[0],
        end: classLines[1],
        label: simpleName(cls.fqn ?? ''),
        kind: 'class',
      });
    }

    for (const method of cls.methods ?? []) {
      const methodLines = method.lines;

      if (methodLines === undefined || methodLines[1] - methodLines[0] < 3) continue;

      regions.push({
        start: methodLines[0],
        end: methodLines[1],
        label: method.signature ?? method.name ?? '',
        kind: 'method',
      });
    }
  }

  return regions;
}

/** 某一行属于哪个符号：优先最内层的方法，其次类；都不在返回 null。 */
function symbolAt(outline: Outline | null, line: number): string | null {
  if (outline === null) return null;

  let best: { label: string; span: number } | null = null;

  for (const cls of outline.classes) {
    for (const method of cls.methods ?? []) {
      const lines = method.lines;

      if (lines === undefined || line < lines[0] || line > lines[1]) continue;

      const span = lines[1] - lines[0];

      if (best === null || span < best.span) {
        best = { label: `${simpleName(cls.fqn ?? '')}#${method.name ?? ''}`, span };
      }
    }

    const classLines = cls.lines;

    if (classLines === undefined || line < classLines[0] || line > classLines[1]) continue;

    const span = classLines[1] - classLines[0];

    if (best === null || span < best.span) {
      best = { label: simpleName(cls.fqn ?? ''), span };
    }
  }

  return best?.label ?? null;
}

/** 「只看引用」时上下各留多少行上下文。 */
const FOCUS_CONTEXT_LINES = 30;

function FileViewer({
  slice,
  highlight,
  focusLine,
  wrap,
  focusOnly,
  outline,
  onVisibleLine,
  scrollTo,
}: {
  slice: FileSlice;
  highlight: readonly number[];
  focusLine: number;
  wrap: boolean;
  focusOnly: boolean;
  outline: Outline | null;
  onVisibleLine: (line: number) => void;
  scrollTo: { line: number; nonce: number } | null;
}) {
  // 按换行切行；`split` 的入参是单字符换行符。
  const lines = useMemo(() => slice.content.split(NEWLINE), [slice]);
  const startLine = slice.start;

  const { palette } = useTheme();
  // 引用区间底色按模式取：浅色亮黄、暗色低饱和暗绿（见 `HIGHLIGHT_DARK`）。
  const highlightBg = palette.mode === 'dark' ? HIGHLIGHT_DARK : palette.warning.light;
  const scrollRef = useRef<HTMLDivElement | null>(null);

  // 整文件只高亮一次；key 是文件内容，滚动不改变它（见 `useHighlightedLines`）。
  const highlighted = useHighlightedLines(
    slice.content,
    languageForPath(slice.path),
    themeForMode(palette.mode)
  );

  const regions = useMemo(
    () => foldableRegions(lines, startLine, outline),
    [lines, startLine, outline]
  );
  // 折叠状态按「区间起始行号」记；默认把 import 折叠起来（和 IDEA 一致）。
  const [folded, setFolded] = useState<readonly number[]>([]);

  useEffect(() => {
    // 默认只折 import：类和方法的折叠按钮交给用户按需点（全折起来只剩几行标题，反而吓人）。
    setFolded(regions.filter((region) => region.kind === 'import').map((region) => region.start));
  }, [regions]);

  // 引用行落在折叠区间里时必须展开它，否则打开抽屉看到的是「import 已折叠」而不是代码。
  useEffect(() => {
    setFolded((current) =>
      current.filter(
        (start) =>
          !regions.some(
            (region) =>
              region.start === start && focusLine >= region.start && focusLine <= region.end
          )
      )
    );
  }, [focusLine, regions]);

  const frame = useRef<number | null>(null);

  // 滚动时算出「顶部可见行」：按 `offsetTop` 二分（换行时行高不定，不能按固定行高乘）。
  const handleScroll = () => {
    if (frame.current !== null) return;

    frame.current = requestAnimationFrame(() => {
      frame.current = null;
      const element = scrollRef.current;

      if (element === null) return;

      const rows = element.querySelectorAll<HTMLElement>('[data-line]');
      let low = 0;
      let high = rows.length - 1;
      let found: HTMLElement | null = null;

      while (low <= high) {
        const middle = (low + high) >> 1;
        const row = rows[middle];

        if (row.offsetTop <= element.scrollTop + 8) {
          found = row;
          low = middle + 1;
        } else {
          high = middle - 1;
        }
      }

      const line = found?.dataset.line;

      if (line !== undefined) onVisibleLine(Number(line));
    });
  };

  const foldedStarts = useMemo(() => new Set(folded), [folded]);
  const regionByStart = useMemo(
    () => new Map(regions.map((region) => [region.start, region])),
    [regions]
  );

  const focusWindow = focusOnly
    ? ([highlight[0] - FOCUS_CONTEXT_LINES, highlight[1] + FOCUS_CONTEXT_LINES] as const)
    : null;

  // 生成要渲染的行：折叠区间收成一行摘要；「只看引用」再把窗口外的行跳过。
  const rows: Array<
    | { kind: 'line'; lineNo: number; index: number; text: string; foldable: FoldRegion | null }
    | { kind: 'fold'; region: FoldRegion }
  > = [];

  for (let index = 0; index < lines.length; index += 1) {
    const lineNo = startLine + index;
    const region = regionByStart.get(lineNo) ?? null;

    if (region !== null && foldedStarts.has(region.start)) {
      rows.push({ kind: 'fold', region });
      index += region.end - region.start;
      continue;
    }

    if (focusWindow !== null && (lineNo < focusWindow[0] || lineNo > focusWindow[1])) continue;

    rows.push({ kind: 'line', lineNo, index, text: lines[index], foldable: region });
  }

  const hiddenByFocus = focusOnly
    ? lines.length - rows.filter((row) => row.kind === 'line').length
    : 0;

  useEffect(() => {
    const element = scrollRef.current;

    if (element === null) return;

    // 打开时滚到第一处高亮：留 35% 视口高度在上方，做到「居中偏上」。
    // 用行元素的 `offsetTop` 而不是「行号 × 固定行高」——开了自动换行时一行可能占多行高，
    // 拿固定行高算会滚偏。
    const row = element.querySelector<HTMLElement>(`[data-line="${focusLine}"]`);

    if (row === null) return;

    element.scrollTop = Math.max(0, row.offsetTop - element.clientHeight * 0.35);
    // 不依赖 `rows.length`：折叠会改变行数，如果这里跟着重跑，会把刚补偿好的视口又拽回引用行。
  }, [focusLine, startLine, slice.content]);

  // 折叠会一次性插入/移除几十行，`scrollTop` 是绝对像素值——不补偿的话视口会跳到别处。
  // 记下「点击的那一行」相对视口的位置，渲染完成后把它放回原位。
  const anchor = useRef<{ lineNo: number; offset: number } | null>(null);

  // 大纲点击跳转：目标行若在折叠区间里，先展开它，再滚过去。
  const pendingJump = useRef<number | null>(null);

  useEffect(() => {
    if (scrollTo === null) return;

    pendingJump.current = scrollTo.line;
    setFolded((current) =>
      current.filter((start) => {
        const region = regions.find((item) => item.start === start);

        return region === undefined || scrollTo.line < region.start || scrollTo.line > region.end;
      })
    );
  }, [scrollTo, regions]);

  useLayoutEffect(() => {
    const line = pendingJump.current;
    const element = scrollRef.current;

    if (line === null || element === null) return;

    const row = element.querySelector<HTMLElement>(`[data-line="${line}"]`);

    if (row === null) return;

    element.scrollTop = Math.max(0, row.offsetTop - element.clientHeight * 0.3);
    pendingJump.current = null;
  }, [folded, scrollTo]);

  const toggleFold = (start: number) => {
    const element = scrollRef.current;
    const row = element?.querySelector<HTMLElement>(`[data-line="${start}"]`);

    if (element !== null && row !== null && row !== undefined) {
      anchor.current = { lineNo: start, offset: row.offsetTop - element.scrollTop };
    }

    setFolded((current) =>
      current.includes(start) ? current.filter((item) => item !== start) : [...current, start]
    );
  };

  // 用 layout effect：在浏览器绘制前校正，否则会先看到一次跳动。
  useLayoutEffect(() => {
    const element = scrollRef.current;
    const pending = anchor.current;

    if (element === null || pending === null) return;

    anchor.current = null;
    const row = element.querySelector<HTMLElement>(`[data-line="${pending.lineNo}"]`);

    if (row !== null) element.scrollTop = Math.max(0, row.offsetTop - pending.offset);
  }, [folded]);

  return (
    <Box
      ref={scrollRef}
      onScroll={handleScroll}
      sx={{
        flex: '1 1 auto',
        minHeight: 0,
        overflow: 'auto',
        // 深色下用 `background.default`（比纸面更暗）才是「凹陷」；`action.hover` 是叠加变亮，
        // 会把代码区推得比抽屉本身还白，正是「太灰白」的来源。
        bgcolor: (theme) => (theme.palette.mode === 'dark' ? 'background.default' : 'action.hover'),
        fontFamily: 'monospace',
        fontSize: 13,
      }}
    >
      {focusOnly && hiddenByFocus > 0 && (
        <Box
          sx={{
            position: 'sticky',
            top: 0,
            zIndex: 1,
            px: 1.5,
            py: 0.5,
            fontSize: 12,
            color: 'text.secondary',
            bgcolor: 'background.paper',
            borderBottom: '1px solid',
            borderColor: 'divider',
          }}
        >
          只看引用 ±{FOCUS_CONTEXT_LINES} 行（隐藏了 {hiddenByFocus} 行，关掉开关可看全文）
        </Box>
      )}

      {rows.map((row) => {
        if (row.kind === 'fold') {
          const { region } = row;

          return (
            <Box
              key={`fold-${region.start}`}
              data-line={region.start}
              onClick={() => toggleFold(region.start)}
              sx={{
                display: 'flex',
                alignItems: 'center',
                gap: 1,
                px: 1,
                height: LINE_HEIGHT,
                lineHeight: `${LINE_HEIGHT}px`,
                cursor: 'pointer',
                color: 'text.secondary',
                fontStyle: 'italic',
                bgcolor: 'action.hover',
                borderTop: '1px solid',
                borderBottom: '1px solid',
                borderColor: 'divider',
                '&:hover': { bgcolor: 'action.selected' },
              }}
            >
              <Box component="span" sx={{ width: 12, textAlign: 'center' }}>
                ▸
              </Box>
              <Box component="span">
                {region.label} …（{region.end - region.start + 1} 行，第 {region.start}-{region.end}{' '}
                行）
              </Box>
            </Box>
          );
        }

        const { lineNo, text, index, foldable } = row;
        const marked = lineNo >= highlight[0] && lineNo <= highlight[1];
        const canFold = foldable !== null && foldable.start === lineNo;

        return (
          <Box
            key={lineNo}
            data-line={lineNo}
            sx={{
              display: 'flex',
              // 交给浏览器跳过屏幕外的行：换行时行高不定，JS 算不了，交给 `content-visibility`
              // 最稳（它同时省掉屏幕外的布局与绘制）。`contain-intrinsic-size` 给出占位高度，
              // 没有它滚动条长度会在渲染过程中反复跳动。
              contentVisibility: 'auto',
              containIntrinsicSize: `auto ${LINE_HEIGHT}px`,
              lineHeight: `${LINE_HEIGHT}px`,
              bgcolor: marked ? highlightBg : 'transparent',
            }}
          >
            <Box
              component="span"
              onClick={canFold ? () => toggleFold(lineNo) : undefined}
              sx={{
                width: 14,
                flexShrink: 0,
                textAlign: 'center',
                color: 'text.secondary',
                cursor: canFold ? 'pointer' : 'default',
                userSelect: 'none',
                opacity: canFold ? 1 : 0,
                '&:hover': { color: 'text.primary' },
              }}
            >
              ▾
            </Box>

            <Box
              component="span"
              sx={{
                width: 52,
                flexShrink: 0,
                pr: 1.5,
                textAlign: 'right',
                // 用 `text.secondary` 而不是 `text.disabled`：灰色 disabled 在高亮行
                // （`warning.light`）上只有 1.96:1，行号会消失。暗色下 `text.secondary` 是
                // `#95A1B2`，在高亮行 `#2C313C` 上 4.52:1、在普通行底上 5.88:1。
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
                // 换行开关：关掉时用 pre 保持列对齐，横向滚动由容器兜住。
                whiteSpace: wrap ? 'pre-wrap' : 'pre',
                overflowWrap: wrap ? 'anywhere' : 'normal',
                pr: 1,
              }}
            >
              <CodeText line={text} tokens={highlighted?.[index] ?? null} />
            </Box>
          </Box>
        );
      })}
    </Box>
  );
}

/**
 * 大纲侧栏：类 + 方法的跳转列表（等价于 IDEA 的 Structure 面板）。
 *
 * 只渲染一层缩进（类 → 方法）：类里的内部类本身就是独立的 `classes` 条目，再嵌套反而更难扫。
 */
function OutlinePanel({
  outline,
  currentLine,
  onJump,
}: {
  outline: Outline;
  currentLine: number | null;
  onJump: (line: number) => void;
}) {
  return (
    <Box
      component="nav"
      sx={{
        width: 230,
        flexShrink: 0,
        display: { xs: 'none', md: 'block' },
        overflowY: 'auto',
        borderLeft: '1px solid',
        borderColor: 'divider',
        bgcolor: 'background.paper',
        py: 1,
      }}
    >
      <Typography variant="caption" sx={{ px: 1.5, color: 'text.secondary' }}>
        结构（{outline.classes.length} 类）
      </Typography>

      {outline.classes.map((cls) => {
        const classLines = cls.lines;

        return (
          <Box key={cls.fqn ?? ''} sx={{ mt: 0.5 }}>
            <OutlineItem
              label={simpleName(cls.fqn ?? '')}
              line={classLines?.[0]}
              active={
                currentLine !== null &&
                classLines !== undefined &&
                currentLine >= classLines[0] &&
                currentLine <= classLines[1]
              }
              bold
              onJump={onJump}
            />

            {(cls.methods ?? []).map((method) => {
              const lines = method.lines;

              return (
                <OutlineItem
                  key={`${cls.fqn}#${method.name}-${lines?.[0] ?? 0}`}
                  label={method.name ?? ''}
                  line={lines?.[0]}
                  active={
                    currentLine !== null &&
                    lines !== undefined &&
                    currentLine >= lines[0] &&
                    currentLine <= lines[1]
                  }
                  onJump={onJump}
                />
              );
            })}
          </Box>
        );
      })}
    </Box>
  );
}

function OutlineItem({
  label,
  line,
  active,
  bold = false,
  onJump,
}: {
  label: string;
  line: number | undefined;
  active: boolean;
  bold?: boolean;
  onJump: (line: number) => void;
}) {
  return (
    <Box
      onClick={line === undefined ? undefined : () => onJump(line)}
      sx={{
        px: 1.5,
        pl: bold ? 1.5 : 3,
        py: 0.25,
        fontSize: 12,
        fontFamily: 'monospace',
        fontWeight: bold ? 600 : 400,
        color: active ? 'primary.main' : 'text.secondary',
        cursor: line === undefined ? 'default' : 'pointer',
        whiteSpace: 'nowrap',
        overflow: 'hidden',
        textOverflow: 'ellipsis',
        bgcolor: active ? 'action.selected' : 'transparent',
        '&:hover': { bgcolor: 'action.hover' },
      }}
      title={label}
    >
      {label}
      {line !== undefined && (
        <Box component="span" sx={{ ml: 1, opacity: 0.6 }}>
          {line}
        </Box>
      )}
    </Box>
  );
}

/**
 * 把 symbol 里与文件路径重复的部分去掉。
 *
 * `net.minecraft.world.level.LocalMobCapCalculator.MobCounts` 在
 * `net/minecraft/world/level/LocalMobCapCalculator.java` 这个文件里——上半段是同一条信息，
 * 标题行已经写了路径，chip 里再画一遍只是噪声。只保留相对文件的那一截（`MobCounts`）。
 */
function shortenSymbol(symbol: string, path: string): string {
  const [owner, method] = symbol.split('#');
  const ownerPath = owner.replace(/\./g, '/');
  const filePath = path.replace(/\.java$/, '');
  const short = ownerPath.startsWith(`${filePath}/`)
    ? ownerPath.slice(filePath.length + 1).replace(/\//g, '.')
    : owner;

  return method === undefined ? short : `${short}#${method}`;
}

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
