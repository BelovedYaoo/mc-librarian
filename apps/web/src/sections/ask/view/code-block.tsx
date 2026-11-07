/**
 * 回答里的 fenced 代码块：行号、语言标签、复制、diff 着色、限高内滚、按内容自适应宽度。
 *
 * 为什么用 Shiki 而不是 `rehype-highlight`：后者的 Java 语法**不给方法调用着色**
 * （`getOrDefault`、`getMaxInstancesPerChunk` 全是正文色），短片段看起来就是一片灰。
 * 抽屉里的源码用的是 Shiki + `Bemylover.icls` 主题，这里复用同一套，两处观感一致。
 *
 * 宽度：`width: fit-content` —— 单行片段只占内容那么宽，长行再撑满并横向滚动。
 *
 * 行号在组件里渲染（不用 CSS counter）：行是一次性排好的 `span.code-line`，行号是独立的
 * 不可选列，复制走原始 `code` 字符串，不会把行号带进去。
 */

import type { ThemedToken } from 'shiki/core';

import { useEffect, useRef, useState } from 'react';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Typography from '@mui/material/Typography';
import { useTheme } from '@mui/material/styles';

import type { HighlightLanguage } from './shiki-highlighter';

import { highlightLines, themeForMode } from './shiki-highlighter';

// ----------------------------------------------------------------------

/** 换行符常量：字面量写在本仓的编辑链里被吞过几次，统一走 `fromCharCode`。 */
const NEWLINE = String.fromCharCode(10);
/** 行高（px）：13px 字号配 22px，中文注释与英文代码都不会挤。 */
const LINE_HEIGHT = 22;
/** 高亮的防抖窗口：流式输出时代码块每个 delta 都在变，逐次高亮会白烧 CPU。 */
const HIGHLIGHT_DEBOUNCE_MS = 120;

/** `language-java` → `java`；其它一律按纯文本。 */
function normalizeLanguage(raw: string): HighlightLanguage | 'text' {
  const name = raw.toLowerCase();

  if (name === 'java') return 'java';

  if (name === 'json') return 'json';

  return 'text';
}

/** Shiki 的 `fontStyle` 是位掩码（1=italic、2=bold）。 */
function fontStyleOf(style: number | undefined): string | undefined {
  if (style === undefined) return undefined;

  const parts: string[] = [];
  if (style & 1) parts.push('italic');
  if (style & 2) parts.push('bold');

  return parts.length > 0 ? parts.join(' ') : undefined;
}

/** 一行代码的渲染数据：原始文本（判 diff 前缀）+ 高亮后的 token（未高亮时为 null）。 */
type RenderedLine = { text: string; tokens: ThemedToken[] | null };

export function MarkdownCode({ code, language }: { code: string; language: string }) {
  const { palette } = useTheme();
  const [lines, setLines] = useState<RenderedLine[]>(() => plainLines(code));
  const [copied, setCopied] = useState(false);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  useEffect(() => {
    const lang = normalizeLanguage(language);

    // 先立刻给一份纯文本：高亮是异步的（Shiki 的 chunk 首次还要下载），不能让代码块空着。
    setLines(plainLines(code));

    if (lang === 'text') return undefined;

    if (timer.current !== null) clearTimeout(timer.current);

    timer.current = setTimeout(() => {
      highlightLines(code, lang, themeForMode(palette.mode))
        .then((tokens) => {
          setLines(
            code.split(NEWLINE).map((text, index) => ({
              text,
              tokens: tokens[index] ?? null,
            }))
          );
        })
        .catch(() => {
          // 高亮失败就停在纯文本，不影响阅读与复制。
        });
    }, HIGHLIGHT_DEBOUNCE_MS);

    return () => {
      if (timer.current !== null) clearTimeout(timer.current);
    };
  }, [code, language, palette.mode]);

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(code);
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
        // 短片段不铺满整行；长行仍被 `maxWidth` 收住，由内层横向滚动。
        width: 'fit-content',
        maxWidth: '100%',
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

        <Box sx={{ flex: '1 1 auto', minWidth: 24 }} />

        <Button size="small" onClick={() => void copy()} sx={{ minWidth: 0, fontSize: 12 }}>
          {copied ? '已复制' : '复制'}
        </Button>
      </Box>

      {/* 内边距放在滚动容器上：markdown 容器的 `& pre { p: 0 }` 特异性更高，写在 `pre` 上会被盖掉。 */}
      <Box sx={{ maxHeight: 420, overflow: 'auto', px: 1.5, py: 1 }}>
        <Box component="pre" sx={{ m: 0, fontSize: 13, lineHeight: `${LINE_HEIGHT}px` }}>
          {lines.map((line, index) => {
            const kind = lineKind(line.text);

            return (
              <Box
                component="span"
                key={index}
                className={kind === '' ? 'code-line' : `code-line ${kind}`}
                sx={{
                  display: 'flex',
                  // 空行也要占一行、也要有行号。
                  minHeight: `${LINE_HEIGHT}px`,
                  borderRadius: 0.5,
                }}
              >
                <Box
                  component="span"
                  sx={{
                    width: '2.8em',
                    flexShrink: 0,
                    pr: 1.5,
                    textAlign: 'right',
                    color: 'text.secondary',
                    opacity: 0.6,
                    userSelect: 'none',
                  }}
                >
                  {index + 1}
                </Box>

                <Box component="span" sx={{ whiteSpace: 'pre' }}>
                  {line.tokens === null
                    ? line.text
                    : line.tokens.map((token, position) => (
                        <span
                          key={position}
                          style={{
                            color: token.color,
                            fontStyle: fontStyleOf(token.fontStyle),
                          }}
                        >
                          {token.content}
                        </span>
                      ))}
                </Box>
              </Box>
            );
          })}
        </Box>
      </Box>
    </Box>
  );
}

/** 纯文本行（高亮未就绪 / 语言不支持时）。 */
function plainLines(code: string): RenderedLine[] {
  return code.split(NEWLINE).map((text) => ({ text, tokens: null }));
}

/**
 * 行分类：hunk 头（`@@`）、新增（`+`）、删除（`-`）。
 *
 * `+++` / `---` 是 diff 的文件头，不算增删。
 */
function lineKind(text: string): string {
  if (/^\+\+\+|^---/.test(text)) return '';

  if (text.startsWith('@@')) return 'diff-hunk';

  if (text.startsWith('+')) return 'diff-add';

  if (text.startsWith('-')) return 'diff-del';

  return '';
}
