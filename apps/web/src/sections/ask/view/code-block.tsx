/**
 * 回答里的 fenced 代码块：行号、语言标签、复制、diff 着色、限高内滚。
 *
 * 为什么不直接用 shadcn / beui 的 `code-block`：它们都是 Tailwind + shadcn 的组件，本项目是
 * MUI + `sx`，装进来等于再引一套样式系统（和 beautifului 那次同一个问题）。这里只借它们的
 * **交互设计**，用 MUI 重写，约 150 行。
 *
 * 行号为什么放在 rehype 阶段做：markdown 的代码块在 React 里是一棵 `span` 树（highlight.js
 * 已经把 token 拆开了），在组件层没法按行切。这里加一个轻量插件，把 `<pre><code>` 的子节点
 * 按换行摊平成「一行一个 `span.code-line`」，行号交给 CSS counter，token 的 `hljs-*` 类原样保留。
 */

import type { ReactNode } from 'react';

import { useRef, useState } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Typography from '@mui/material/Typography';

// ----------------------------------------------------------------------

/**
 * 换行符常量：字面量写在本仓的编辑链里被吞过几次（Python heredoc 会把 `
` 变成真换行），
 * 统一走 `fromCharCode`，不再依赖转义。
 */
const NEWLINE = String.fromCharCode(10);

/** hast 节点的最小结构（只覆盖本文件用到的字段）。 */
type HastNode = {
  type: string;
  tagName?: string;
  value?: string;
  properties?: Record<string, unknown>;
  children?: HastNode[];
};

/**
 * 把若干 hast 子节点按换行切成「每行一组」，**保留 token 的 span 外壳**。
 *
 * 关键点：换行可能落在某个 `<span class="hljs-string">` 内部（多行字符串/注释），
 * 这时要把该 span 拆成多个克隆，各自带走自己那一行的文本——不能直接把它拍平成文本，
 * 否则高亮就丢了。
 */
function splitChildren(children: HastNode[]): HastNode[][] {
  const lines: HastNode[][] = [[]];

  for (const child of children) {
    if (child.type === 'text') {
      const parts = (child.value ?? '').split(NEWLINE);

      parts.forEach((part, index) => {
        if (index > 0) lines.push([]);
        if (part !== '') lines[lines.length - 1].push({ type: 'text', value: part });
      });

      continue;
    }

    const inner = splitChildren(child.children ?? []);

    inner.forEach((part, index) => {
      if (index > 0) lines.push([]);

      if (part.length > 0) {
        lines[lines.length - 1].push({ ...child, children: part });
      }
    });
  }

  return lines;
}

/** 一行里的纯文本（用于判断 diff 前缀与空行）。 */
function textOf(nodes: HastNode[]): string {
  return nodes
    .map((node) => (node.type === 'text' ? (node.value ?? '') : textOf(node.children ?? [])))
    .join('');
}

/**
 * 把 `<pre><code>` 的内容按行摊平，并给 `<pre>` 打上 `data-language`。
 *
 * 只处理 `pre > code`（fenced 代码块）：行内 `code` 不受影响。
 */
export function rehypeCodeLines() {
  return (tree: HastNode) => {
    const visit = (node: HastNode) => {
      if (node.type === 'element' && node.tagName === 'pre') {
        const code = (node.children ?? []).find(
          (child) => child.type === 'element' && child.tagName === 'code'
        );

        if (code !== undefined) {
          const className = String((code.properties?.className ?? []).toString());
          const language = /language-([\w-]+)/.exec(className)?.[1] ?? 'text';

          node.properties = { ...node.properties, 'data-language': language };

          const lines = splitChildren(code.children ?? []);

          // 围栏内容常以换行结尾（最后一行只有换行符），会多出一个空行——它不该占一个行号。
          while (lines.length > 1 && lines[lines.length - 1].length === 0) lines.pop();

          code.children = lines.map((children) => {
            const text = textOf(children);
            const kind =
              /^\+\+\+|^---/.test(text) ? '' : text.startsWith('+') ? 'diff-add' : text.startsWith('-') ? 'diff-del' : '';

            return {
              type: 'element',
              tagName: 'span',
              properties: { className: ['code-line', kind].filter(Boolean) },
              children,
            };
          });
        }
      }

      for (const child of node.children ?? []) visit(child);
    };

    visit(tree);
  };
}

// ----------------------------------------------------------------------

/**
 * fenced 代码块的外壳：顶部一条 header（语言 + 复制），内容限高内滚。
 *
 * 复制取的是 `<pre>` 的 `innerText`：行号是 CSS `::before` 生成的，不在 `innerText` 里，
 * 所以复制结果是干净的代码。
 */
export function CodeBlockPre({
  node,
  children,
  ...props
}: {
  node?: HastNode;
  children?: ReactNode;
  [key: string]: unknown;
}) {
  const preRef = useRef<HTMLPreElement | null>(null);
  const [copied, setCopied] = useState(false);
  const language = String(node?.properties?.['data-language'] ?? 'text');

  const copy = async () => {
    const text = preRef.current?.innerText ?? '';

    try {
      await navigator.clipboard.writeText(text);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    } catch {
      setCopied(false);
    }
  };

  return (
    <Box
      sx={{
        my: 1.5,
        border: '1px solid',
        borderColor: 'divider',
        borderRadius: 1.5,
        overflow: 'hidden',
        bgcolor: 'background.neutral',
      }}
    >
      <Box
        sx={{
          display: 'flex',
          alignItems: 'center',
          gap: 1,
          px: 1.5,
          py: 0.5,
          borderBottom: '1px solid',
          borderColor: 'divider',
        }}
      >
        <Typography variant="caption" sx={{ color: 'text.secondary', fontFamily: 'monospace' }}>
          {language}
        </Typography>

        <Box sx={{ flex: '1 1 auto' }} />

        <Button size="small" onClick={() => void copy()} sx={{ minWidth: 0, fontSize: 12 }}>
          {copied ? '已复制' : '复制'}
        </Button>
      </Box>

      <Box sx={{ maxHeight: 420, overflow: 'auto' }}>
        <Box component="pre" ref={preRef} {...props} sx={{ m: 0, px: 1.5, py: 1 }}>
          {children}
        </Box>
      </Box>
    </Box>
  );
}
