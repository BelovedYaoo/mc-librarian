import type { Components } from 'react-markdown';

import { Children, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import Box from '@mui/material/Box';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';

import type { Citation } from 'src/lib/api-client';
import { markdownContainerSx } from 'src/theme';

import { MarkdownCode } from './code-block';

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
export function AnswerMarkdown({
  text,
  streaming,
  onOpenCitation,
  citations = [],
}: {
  text: string;
  streaming: boolean;
  /** 点击答案里的 `xxx.java:12-34` 时打开源码抽屉；不传则内联代码保持纯文本。 */
  onOpenCitation?: (citation: Citation) => void;
  /** 本轮引用列表：正文里的裸引用常只写文件名，用它补成完整路径（否则抽屉会 404）。 */
  citations?: Citation[];
}) {
  const components = useMemo<Components>(
    () => buildComponents(onOpenCitation, citations),
    [onOpenCitation, citations]
  );
  const throttled = useThrottledValue(text, STREAM_THROTTLE_MS);
  // `done` 后直接用原文，保证最终内容与后端返回逐字一致（节流的尾帧可能还在路上）。
  const source = streaming ? throttled : text;

  return (
    <Box sx={markdownContainerSx}>
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
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
/**
 * 答案里的代码位置引用：`net/minecraft/.../Foo.java:401-496`、`Foo.java:45`、`...java.patch:12-34`。
 *
 * 只匹配**路径 + 行号**——单纯的 `:466-493`（同上文同一个文件）无法独立定位，保持纯文本。
 */
/** 换行符常量：字面量 `'
'` 在本仓的编辑链里被吞过两次，统一走常量。 */
const NEWLINE = String.fromCharCode(10);

const CODE_REFERENCE = /([\w./$-]+\.(?:java|patch)):(\d+)(?:-(\d+))?/;

/**
 * 裸引用常只写文件名（`NaturalSpawner.java:499-506`），而 `/v1/read` 要的是相对源码根的
 * 完整路径。用本轮引用列表按「路径后缀」补全；补不上就原样返回（抽屉会给出明确的 404）。
 */
function resolvePath(path: string, citations: Citation[]): string {
  if (path.includes('/')) return path;

  const suffix = `/${path}`;
  const matched = citations.find((citation) => citation.path.endsWith(suffix));

  return matched?.path ?? path;
}

/** 从一段文字里抽出第一个可定位的代码引用；抽不到返回 null。 */
function parseCodeReference(text: string, citations: Citation[] = []): Citation | null {
  const matched = CODE_REFERENCE.exec(text);

  if (matched === null) return null;

  const start = Number(matched[2]);
  const end = matched[3] === undefined ? start : Number(matched[3]);

  return { path: resolvePath(matched[1], citations), lines: [start, end] };
}

const CODE_REFERENCE_ALL = new RegExp(CODE_REFERENCE.source, 'g');

/** 引用片段的统一样式：点线 + 手型，悬停有底色。内联代码与纯文本共用。 */
const referenceSx = {
  cursor: 'pointer',
  textDecoration: 'underline dotted',
  textUnderlineOffset: '3px',
  '&:hover': { backgroundColor: 'action.selected' },
} as const;

/**
 * 把一段**纯文本**里的代码引用拆成「文本 + 可点击片段」。
 *
 * 模型写的引用有两种形态：包在反引号里的行内代码（由 `code` 渲染器处理），以及直接写在
 * 正文里的裸路径（`NaturalSpawner.java:499-506`）。后者更常见，不处理的话正文里绝大多数
 * 位置都点不开。
 */
function splitReferences(
  text: string,
  onOpenCitation: (citation: Citation) => void,
  citations: Citation[]
): ReactNode[] {
  const nodes: ReactNode[] = [];
  let cursor = 0;

  for (const matched of text.matchAll(CODE_REFERENCE_ALL)) {
    const start = matched.index ?? 0;

    if (start > cursor) nodes.push(text.slice(cursor, start));

    const citation: Citation = {
      path: resolvePath(matched[1], citations),
      lines: [Number(matched[2]), matched[3] === undefined ? Number(matched[2]) : Number(matched[3])],
    };

    nodes.push(
      <Box
        component="span"
        key={`ref-${start}`}
        onClick={() => onOpenCitation(citation)}
        title={`打开 ${citation.path}:${citation.lines.join('-')}`}
        sx={referenceSx}
      >
        {matched[0]}
      </Box>
    );

    cursor = start + matched[0].length;
  }

  if (cursor < text.length) nodes.push(text.slice(cursor));

  return nodes;
}

/** 把子节点里的纯文本过一遍 `splitReferences`；元素节点原样保留。 */
function withReferences(
  children: ReactNode,
  onOpenCitation: ((citation: Citation) => void) | undefined,
  citations: Citation[]
): ReactNode {
  if (onOpenCitation === undefined) return children;

  return Children.map(children, (child) =>
    typeof child === 'string' ? splitReferences(child, onOpenCitation, citations) : child
  );
}

/** markdown 渲染器：表格横向可滚；内联代码里带路径+行号的做成可点击。 */
function buildComponents(
  onOpenCitation?: (citation: Citation) => void,
  citations: Citation[] = []
): Components {
  return {
    table: ({ node: _node, ...props }) => (
      <Box sx={{ overflowX: 'auto', my: 1.5 }}>
        <table {...props} />
      </Box>
    ),
    // 代码块由 `code` 组件自己渲染（语言标签 + 行号 + 复制 + Shiki 高亮），
    // 所以 `pre` 只做透传，避免外面再套一层 `<pre>`。
    pre: ({ node: _node, children }) => <>{children}</>,
    // 段落与列表项是引用出现最多的地方：把其中的裸路径也变成可点击。
    p: ({ node: _node, children, ...props }) => (
      <p {...props}>{withReferences(children, onOpenCitation, citations)}</p>
    ),
    li: ({ node: _node, children, ...props }) => (
      <li {...props}>{withReferences(children, onOpenCitation, citations)}</li>
    ),
    code: ({ node: _node, className, children, ...props }) => {
      const raw = String(children ?? '');
      const language = /language-([\w-]+)/.exec(className ?? '')?.[1];
      const isBlock = language !== undefined || raw.includes(NEWLINE);

      if (isBlock) {
        // 围栏内容以换行结尾，去掉尾部那一个：否则会多出一个空行行号。
        return <MarkdownCode code={raw.replace(/\n$/, '')} language={language ?? 'text'} />;
      }

      const citation = onOpenCitation !== undefined ? parseCodeReference(raw, citations) : null;

      if (citation === null) {
        return (
          <code className={className} {...props}>
            {children}
          </code>
        );
      }

      return (
        <Box
          component="code"
          className={className}
          {...props}
          onClick={() => onOpenCitation?.(citation)}
          title={`打开 ${citation.path}:${citation.lines.join('-')}`}
          // 点线而不是实线下划线：与普通内联代码的观感拉开一档，又不至于像链接那么抢眼。
          sx={referenceSx}
        >
          {children}
        </Box>
      );
    },
  };
}

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
