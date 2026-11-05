import type { Components } from 'react-markdown';

import { useEffect, useRef, useState } from 'react';
import Box from '@mui/material/Box';
import ReactMarkdown from 'react-markdown';
import rehypeHighlight from 'rehype-highlight';
import remarkGfm from 'remark-gfm';

import { markdownContainerSx } from 'src/theme';

// ----------------------------------------------------------------------

/** ~80ms 的节流窗口：足够把几十个 delta 合并成几次渲染，又不至于看出卡顿。 */
const STREAM_THROTTLE_MS = 80;

/**
 * 流式答案的 markdown 渲染（规格：`##` 标题、表格、代码块、列表都要可读）。
 *
 * 三个流式专属的处理：
 * 1. 节流：delta 可能每秒几十个，`useThrottledValue` 把重渲染压到 ~80ms 一次（首帧立即出，
 *    尾帧补一次），不是每个 delta 都重解析整篇。
 * 2. 未闭合围栏：流式中间态常出现只有开头 ``` 没有结尾，渲染前补一个临时闭合（不改原文本）。
 * 3. 光标：`streaming` 时在末尾显示一个闪烁方块，`done` 后消失。
 */
export function AnswerMarkdown({ text, streaming }: { text: string; streaming: boolean }) {
  const throttled = useThrottledValue(text, STREAM_THROTTLE_MS);
  // `done` 后直接用原文，保证最终内容与后端返回逐字一致（节流的尾帧可能还在路上）。
  const source = streaming ? throttled : text;

  return (
    <Box sx={markdownContainerSx}>
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        components={markdownComponents}
      >
        {closeOpenFence(source)}
      </ReactMarkdown>

      {streaming && <Box component="span" aria-hidden sx={cursorSx} />}
    </Box>
  );
}

/**
 * 首帧 + 尾帧节流：值一变就（距上次刷新够久时）立刻渲染一次，否则排一个定时器到窗口末尾再补一次。
 * 定时器只排一个，不因后续 delta 重置——否则就成了 debounce，持续输出时永远等不到尾帧。
 */
function useThrottledValue<T>(value: T, delayMs: number): T {
  const [throttled, setThrottled] = useState(value);
  const latest = useRef(value);
  const lastFlush = useRef(0);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    latest.current = value;

    const elapsed = Date.now() - lastFlush.current;

    if (elapsed >= delayMs) {
      lastFlush.current = Date.now();
      setThrottled(latest.current);

      return undefined;
    }

    if (timer.current !== null) return undefined;

    timer.current = setTimeout(() => {
      timer.current = null;
      lastFlush.current = Date.now();
      setThrottled(latest.current);
    }, delayMs - elapsed);

    return undefined;
  }, [value, delayMs]);

  useEffect(
    () => () => {
      if (timer.current !== null) clearTimeout(timer.current);
    },
    []
  );

  return throttled;
}

/** 围栏 ``` 数为奇数时说明还开着：补一个闭合行；只影响渲染，不写回 `turn.answer`。 */
function closeOpenFence(markdown: string): string {
  const fences = markdown.split('\n').filter((line) => line.trimStart().startsWith('```')).length;

  return fences % 2 === 1 ? `${markdown}\n\`\`\`` : markdown;
}

// ----------------------------------------------------------------------

/**
 * 表格需要横向滚动容器；其余元素靠 `markdownContainerSx` 的后代选择器统一上样式。
 */
const markdownComponents: Components = {
  table: ({ node: _node, ...props }) => (
    <Box sx={{ overflowX: 'auto', my: 1.5 }}>
      <table {...props} />
    </Box>
  ),
};

const cursorSx = {
  display: 'inline-block',
  width: '0.55em',
  height: '1.05em',
  ml: 0.25,
  verticalAlign: 'text-bottom',
  bgcolor: 'text.primary',
  animation: 'mc-librarian-cursor-blink 1s steps(2, start) infinite',
  '@keyframes mc-librarian-cursor-blink': { '50%': { opacity: 0 } },
};
