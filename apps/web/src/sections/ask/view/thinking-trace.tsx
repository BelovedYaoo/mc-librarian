/**
 * 工具调用轨迹（移植自 beautifului.dev 的 `Thinking State` → `Coding` 变体，改成 MUI + `sx`）。
 *
 * 那边的交互本质是两条：
 *
 * 1. **进行中**：标题是现在时（「Running tools」）+ 转动的指示器 + 逐步追加的行；
 * 2. **结束后**：整个轨迹**沉淀成一行过去时**（「Ran 3 tools」），明细默认收起但可再展开。
 *
 * 我们原来的实现只有一个 `工具轨迹 3/3` 的 Chip 加一段等宽文本——进行中没有「还在做事」的
 * 信号，结束后也看不出「做了什么」。这里按上面两条重做：
 *
 * - 状态点：进行中 = 像素格动画；成功 = ✓；失败 = ✕
 * - 每行：工具名 + 最关键的那个参数（等宽、可截断）+ 结果（`N 条` / `失败`）
 * - 头部常驻：`正在检索 · 检索源码` / `已调用 N 次工具`，右侧是实时/总耗时。
 *   用「次」不用「步」：`max_steps` 数的是**模型轮次**，一轮里可以并发多个工具调用，
 *   所以工具调用次数往往大于步数（实测一次 `max_steps` 收尾是 12 轮 / 23 次调用）。
 * - 进行中默认展开，结束后自动收起（用户手动展开过就不再自动收）
 */

import { useEffect, useRef, useState } from 'react';
import { alpha, keyframes } from '@mui/material/styles';
import Box from '@mui/material/Box';
import Collapse from '@mui/material/Collapse';
import Stack from '@mui/material/Stack';
import Tooltip from '@mui/material/Tooltip';
import Typography from '@mui/material/Typography';

import type { AskStep } from 'src/lib/api-client';

import { formatSeconds } from './generating-indicator';

const CONTEXT_BUDGET_TOOL = 'context_budget';

/**
 * 明细区的高度上限 = 5 行。一次深度检索会累积 20+ 次工具调用，不封顶时轨迹会把它下面的
 * 答案一路撑下去，一屏全是日志。
 *
 * 行高的来源：`py: 0.625`（上下各 5px）+ 12.5px 字号的一行正文（约 18px）≈ 28px。
 * 换行（`textOverflow: ellipsis` + 单行）不会改变行高，所以可以按固定值算。
 */
const TRACE_ROW_HEIGHT = 28;
const TRACE_VISIBLE_ROWS = 5;

const spin = keyframes`
  to { transform: rotate(360deg); }
`;

export type StepTrace = Pick<AskStep, 'tool'> & {
  args?: Record<string, unknown>;
  ok?: boolean;
  hits?: number;
  /** 失败原因（服务端在 `ok=false` 时带），hover 显示，界面不必只写「失败」。 */
  error?: string;
};

export type ThinkingTraceProps = {
  steps: StepTrace[];
  /** 该轮是否还在流式进行中（决定「正在检索」还是「已完成」）。 */
  streaming: boolean;
  /** 请求开始时间，用于头部计时；`null` 表示不计时。 */
  startedAt: number | null;
  /** 结束后展示的总耗时（`done` 事件给的 `latency_ms`）；没有就用前端计时的值。 */
  latencyMs?: number | null;
};

/** 进行中的指示器：一个小圆环，`prefers-reduced-motion` 下静止。 */
function Spinner() {
  return (
    <Box
      aria-hidden
      sx={(theme) => ({
        width: 12,
        height: 12,
        flexShrink: 0,
        borderRadius: '50%',
        border: '2px solid',
        borderColor: 'text.disabled',
        borderTopColor: theme.palette.primary.main,
        animation: `${spin} 0.8s linear infinite`,
        '@media (prefers-reduced-motion: reduce)': { animation: 'none' },
      })}
    />
  );
}

/** 单步的状态标记。 */
function StepStatus({ step }: { step: StepTrace }) {
  if (step.ok === undefined) return <Spinner />;

  return (
    <Box
      aria-hidden
      sx={{
        width: 12,
        height: 12,
        flexShrink: 0,
        display: 'grid',
        placeItems: 'center',
        borderRadius: '50%',
        fontSize: 9,
        lineHeight: 1,
        color: step.ok ? 'success.main' : 'error.main',
      }}
    >
      {step.ok ? '✓' : '✕'}
    </Box>
  );
}

/**
 * 从参数里挑「最能说明这一步在干什么」的那一项，等宽显示。
 * 全量 `JSON.stringify(args)` 太长（`search` 的 query + 一堆可选参数），在轨迹行里读不动。
 */
function primaryArg(args: Record<string, unknown> | undefined): string | null {
  if (args === undefined) return null;

  for (const key of ['query_text', 'query', 'name', 'symptom', 'symbol_name', 'symbol', 'path']) {
    const value = args[key];

    if (typeof value === 'string' && value !== '') return value;
  }

  return null;
}

export function ThinkingTrace({ steps, streaming, startedAt, latencyMs }: ThinkingTraceProps) {
  // `null` = 用户没表过态，跟随 `streaming` 自动开合；一旦手动点过就听用户的。
  const [pinned, setPinned] = useState<boolean | null>(null);
  const [open, setOpen] = useState(streaming);
  const wasStreaming = useRef(streaming);
  const rowsRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    // 只在「进行中 → 结束」这一刻自动收起；之后用户再展开就不会被重新收起。
    if (wasStreaming.current && !streaming && pinned === null) setOpen(false);
    wasStreaming.current = streaming;
  }, [streaming, pinned]);

  useEffect(() => {
    // 进行中把明细滚到底：最新一步永远可见（像看日志）。
    // 结束后不再动——那时用户在往回翻，被拽到底很烦。
    const element = rowsRef.current;

    if (element !== null && open && streaming) element.scrollTop = element.scrollHeight;
  }, [steps.length, open, streaming]);

  const toolSteps = steps.filter((step) => step.tool !== CONTEXT_BUDGET_TOOL);
  const failed = toolSteps.filter((step) => step.ok === false).length;
  const current = toolSteps.find((step) => step.ok === undefined);

  const total = toolSteps.length;
  const label = streaming
    ? current
      ? `正在检索 · ${describeTool(current.tool)}`
      : '正在规划下一步'
    : failed > 0
      ? `已调用 ${total} 次工具 · ${failed} 次失败`
      : `已调用 ${total} 次工具`;
  const seconds = latencyMs != null ? latencyMs : null;

  return (
    <Box
      sx={{
        border: '1px solid',
        borderColor: 'divider',
        borderRadius: 1.5,
        overflow: 'hidden',
        bgcolor: 'background.paper',
      }}
    >
      <Stack
        direction="row"
        spacing={1}
        onClick={() => {
          setPinned(!open);
          setOpen(!open);
        }}
        sx={{
          alignItems: 'center',
          px: 1.5,
          py: 1,
          cursor: 'pointer',
          userSelect: 'none',
          '&:hover': { bgcolor: 'action.hover' },
        }}
      >
        {streaming ? (
          <Spinner />
        ) : (
          <Box
            aria-hidden
            sx={{
              width: 12,
              height: 12,
              flexShrink: 0,
              display: 'grid',
              placeItems: 'center',
              fontSize: 10,
              color: failed > 0 ? 'error.main' : 'success.main',
            }}
          >
            {failed > 0 ? '✕' : '✓'}
          </Box>
        )}

        <Typography variant="body2" sx={{ fontWeight: 500 }}>
          {label}
        </Typography>

        <Box sx={{ flex: '1 1 auto' }} />

        {/* 计时：进行中走实时值，结束后用服务端给的总耗时（含最后一段生成）。 */}
        <Typography
          variant="caption"
          sx={{
            fontFamily: 'monospace',
            fontVariantNumeric: 'tabular-nums',
            color: 'text.secondary',
          }}
        >
          {streaming ? (
            <LiveSeconds startedAt={startedAt} />
          ) : seconds != null ? (
            formatSeconds(seconds)
          ) : (
            ''
          )}
        </Typography>

        <Box
          aria-hidden
          sx={{
            width: 8,
            height: 8,
            flexShrink: 0,
            borderRight: '1.5px solid',
            borderBottom: '1.5px solid',
            borderColor: 'text.disabled',
            transform: open ? 'rotate(-135deg)' : 'rotate(45deg)',
            transition: 'transform 150ms ease',
            mb: open ? '-3px' : '3px',
          }}
        />
      </Stack>

      <Collapse in={open}>
        {/* 明细区固定高度、内部滚动：一次 `max_steps` 收尾可能产生 20+ 行，不封顶的话轨迹会把它
            下面的答案一路撑下去，消息区被一份日志占满。每行之间一条极淡分隔线——20+ 行没有
            分隔时，横向扫读会串行。 */}
        <Box
          ref={rowsRef}
          sx={{
            maxHeight: TRACE_ROW_HEIGHT * TRACE_VISIBLE_ROWS,
            overflowY: 'auto',
            px: 1.5,
            pb: 1.25,
            pt: 0.25,
            '& > * + *': { borderTop: '1px solid', borderColor: 'divider' },
          }}
        >
          {steps.map((step, index) => (
            <TraceRow key={`${step.tool}-${index}`} step={step} />
          ))}
        </Box>
      </Collapse>
    </Box>
  );
}

/** 头部计时：单独一个组件，避免整条轨迹每 100ms 重渲染。 */
function LiveSeconds({ startedAt }: { startedAt: number | null }) {
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (startedAt === null) return undefined;

    const timer = window.setInterval(() => setNow(Date.now()), 100);

    return () => window.clearInterval(timer);
  }, [startedAt]);

  if (startedAt === null) return null;

  return <>{formatSeconds(now - startedAt)}</>;
}

function TraceRow({ step }: { step: StepTrace }) {
  const isBudget = step.tool === CONTEXT_BUDGET_TOOL;
  const arg = primaryArg(step.args);

  return (
    <Stack
      direction="row"
      spacing={1}
      sx={{
        alignItems: 'center',
        py: 0.625,
        minWidth: 0,
        // 失败行给一层淡红底：只靠右侧一个红字，在 20 多行里会漏看。
        ...(step.ok === false && {
          bgcolor: (theme) =>
            alpha(theme.palette.error.main, theme.palette.mode === 'dark' ? 0.1 : 0.06),
          mx: -1.5,
          px: 1.5,
        }),
      }}
    >
      <StepStatus step={step} />

      <Typography
        variant="body2"
        sx={{ fontFamily: 'monospace', fontSize: 12.5, color: 'text.secondary', flexShrink: 0 }}
      >
        {isBudget ? '压缩上下文' : step.tool}
      </Typography>

      {arg !== null && (
        <Typography
          variant="body2"
          sx={{
            fontFamily: 'monospace',
            fontSize: 12.5,
            color: 'text.primary',
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            minWidth: 0,
          }}
        >
          {arg}
        </Typography>
      )}

      <Box sx={{ flex: '1 1 auto' }} />

      <Tooltip
        title={step.ok === false ? (step.error ?? '失败（服务端未带原因）') : ''}
        placement="left"
      >
        <Typography
          variant="caption"
          sx={{
            fontFamily: 'monospace',
            fontVariantNumeric: 'tabular-nums',
            flexShrink: 0,
            color: step.ok === false ? 'error.main' : 'text.disabled',
            // 失败原因可能很长：加下划线虚线提示「这里可以看」。
            ...(step.ok === false && {
              textDecoration: 'underline dotted',
              cursor: 'help',
            }),
          }}
        >
          {step.ok === undefined ? '…' : step.ok ? `${step.hits ?? 0} 条` : '失败'}
        </Typography>
      </Tooltip>
    </Stack>
  );
}

/** 工具名 → 中文动作（进行中的标题用；未知工具原样显示）。 */
function describeTool(tool: string): string {
  const labels: Record<string, string> = {
    search: '检索源码',
    symbol: '查符号',
    references: '反查引用',
    outline: '读文件骨架',
    read: '读源码',
  };

  return labels[tool] ?? tool;
}
