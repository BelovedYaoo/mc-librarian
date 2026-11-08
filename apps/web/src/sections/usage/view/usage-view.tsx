import type { UsageBucket, UsageGroupBy, UsageReport } from 'src/lib/api-client';

import { useState } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Divider from '@mui/material/Divider';
import LinearProgress from '@mui/material/LinearProgress';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';
import { useTheme } from '@mui/material/styles';

import { useUsage } from 'src/hooks';

// ----------------------------------------------------------------------

/** 聚合维度的顺序与中文名（键名与契约 `getUsage` 的 `group_by` 一一对应）。 */
const GROUP_ORDER: UsageGroupBy[] = ['day', 'user', 'version', 'model'];

const GROUP_LABELS: Record<UsageGroupBy, string> = {
  day: '日期',
  user: '用户',
  version: '版本',
  model: '模型',
};

/** 图表最多画多少个分组：维度切成「用户/模型」时可能几十个，全画会糊成一团。 */
const CHART_LIMIT = 12;

/**
 * `/usage`：管理员查看用量（后端 `GET /v1/admin/usage`）。
 *
 * 三块：KPI（请求数 / token / 平均耗时）+ 堆叠柱状图（prompt vs completion）+ 明细表。
 * 维度（日期 / 用户 / 版本 / 模型）同一个接口切换，数字全部来自服务端聚合，前端不再累加。
 */
export function UsageView() {
  const [from, setFrom] = useState(defaultRange().from);
  const [to, setTo] = useState(defaultRange().to);
  const [groupBy, setGroupBy] = useState<UsageGroupBy>('day');
  const { state, refresh } = useUsage({ from, to, groupBy });

  return (
    <Stack spacing={3}>
      <div>
        <Typography variant="h4" sx={{ mb: 1 }}>
          用量
        </Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          每次问答记一行用量，按本地日期（UTC+8）分天；区间内没有数据时显示零值，不是错误。
        </Typography>
      </div>

      <Card>
        <CardContent>
          <Stack
            direction={{ xs: 'column', sm: 'row' }}
            spacing={2}
            sx={{ alignItems: { sm: 'center' } }}
          >
            <TextField
              type="date"
              label="起始"
              size="small"
              value={from}
              onChange={(event) => setFrom(event.target.value)}
              slotProps={{ inputLabel: { shrink: true } }}
            />

            <TextField
              type="date"
              label="结束"
              size="small"
              value={to}
              onChange={(event) => setTo(event.target.value)}
              slotProps={{ inputLabel: { shrink: true } }}
            />

            <TextField
              select
              label="聚合维度"
              size="small"
              value={groupBy}
              onChange={(event) => setGroupBy(event.target.value as UsageGroupBy)}
              sx={{ minWidth: 140 }}
            >
              {GROUP_ORDER.map((key) => (
                <MenuItem key={key} value={key}>
                  {GROUP_LABELS[key]}
                </MenuItem>
              ))}
            </TextField>

            <Button
              size="small"
              variant="outlined"
              onClick={() => {
                const range = defaultRange();

                setFrom(range.from);
                setTo(range.to);
              }}
            >
              最近 7 天
            </Button>

            <Box sx={{ flex: '1 1 auto' }} />

            {state.status === 'loading' && <CircularProgress size={16} />}

            <Button size="small" variant="outlined" onClick={refresh}>
              刷新
            </Button>
          </Stack>

          <Divider sx={{ my: 2 }} />

          {state.status === 'error' && (
            <Alert severity="error">读取用量失败：{state.message}</Alert>
          )}

          {state.status === 'ready' && <Report report={state.report} groupBy={groupBy} />}
        </CardContent>
      </Card>
    </Stack>
  );
}

// ----------------------------------------------------------------------

function Report({ report, groupBy }: { report: UsageReport; groupBy: UsageGroupBy }) {
  // 后端只返回「有数据的分组」：按天统计时没问答的那天会整个消失，折线/柱状图就断了。
  // 这里按 [from, to] 补齐零值（只在 day 维度补；用户/版本/模型补零没有意义）。
  const buckets = groupBy === 'day' ? fillDays(report.buckets, report.from, report.to) : report.buckets;
  const promptTokens = report.totals.prompt_tokens ?? 0;
  const completionTokens = report.totals.completion_tokens ?? 0;
  const totalTokens = promptTokens + completionTokens;
  const avgTokens = report.requests > 0 ? Math.round(totalTokens / report.requests) : 0;
  const avgLatency =
    report.requests > 0 ? Math.round((report.totals.latency_ms ?? 0) / report.requests) : 0;

  return (
    <Stack spacing={3}>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
        <StatCard label="请求数" value={report.requests.toLocaleString('zh-CN')} />
        <StatCard
          label="总 token"
          value={compactNumber(totalTokens)}
          hint={`prompt ${compactNumber(promptTokens)} · completion ${compactNumber(completionTokens)}`}
        />
        <StatCard label="平均每次 token" value={compactNumber(avgTokens)} />
        <StatCard label="平均耗时" value={formatLatency(avgLatency)} />
      </Stack>

      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        区间 {report.from} → {report.to}（含当天）· 按{GROUP_LABELS[groupBy]}聚合
      </Typography>

      {buckets.length === 0 ? (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          这段时间没有用量数据。
        </Typography>
      ) : (
        <>
          <Stack direction={{ xs: 'column', lg: 'row' }} spacing={2} sx={{ alignItems: 'stretch' }}>
            <Card variant="outlined" sx={{ flex: '1 1 62%', minWidth: 0 }}>
              <CardContent>
                <Stack direction="row" spacing={1} sx={{ alignItems: 'baseline', mb: 1 }}>
                  <Typography variant="subtitle2">token 分布</Typography>
                  <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                    堆叠：prompt + completion
                  </Typography>
                  <Box sx={{ flex: '1 1 auto' }} />
                  {buckets.length > CHART_LIMIT && (
                    <Chip size="small" label={`仅显示前 ${CHART_LIMIT} 组`} />
                  )}
                </Stack>

                <UsageChart buckets={buckets.slice(0, CHART_LIMIT)} />
              </CardContent>
            </Card>

            <Card variant="outlined" sx={{ flex: '1 1 38%', minWidth: 0 }}>
              <CardContent>
                <Typography variant="subtitle2" sx={{ mb: 1.5 }}>
                  占比前 5
                </Typography>

                <TopList buckets={buckets} total={totalTokens} />
              </CardContent>
            </Card>
          </Stack>

          <Box sx={{ overflowX: 'auto' }}>
            <Table size="small">
              <TableHead>
                <TableRow>
                  <TableCell>{GROUP_LABELS[groupBy]}</TableCell>
                  <TableCell align="right">请求数</TableCell>
                  <TableCell align="right">prompt</TableCell>
                  <TableCell align="right">completion</TableCell>
                  <TableCell align="right">合计</TableCell>
                  <TableCell align="right">平均耗时</TableCell>
                  <TableCell sx={{ minWidth: 140 }}>占比</TableCell>
                </TableRow>
              </TableHead>

              <TableBody>
                {buckets.map((bucket) => (
                  <BucketRow key={bucket.key} bucket={bucket} total={totalTokens} />
                ))}
              </TableBody>
            </Table>
          </Box>
        </>
      )}
    </Stack>
  );
}

// ----------------------------------------------------------------------

/**
 * 堆叠柱状图（纯 SVG，不引图表库）。
 *
 * 两段颜色：prompt 用主色、completion 用信息色；柱顶留 8px，避免最高柱贴边。
 * `viewBox` 固定宽高、外层 `width: 100%`，随容器缩放（字号按比例缩放，仍在可读范围）。
 */
function UsageChart({ buckets }: { buckets: UsageBucket[] }) {
  const { palette } = useTheme();
  const width = 800;
  const height = 260;
  const padding = { top: 16, right: 8, bottom: 42, left: 56 };
  const plotWidth = width - padding.left - padding.right;
  const plotHeight = height - padding.top - padding.bottom;

  const totals = buckets.map((bucket) => bucket.prompt_tokens + bucket.completion_tokens);
  const max = Math.max(...totals, 1);
  const slot = plotWidth / buckets.length;
  const barWidth = Math.min(46, Math.max(8, slot * 0.55));

  const ticks = [0, 0.25, 0.5, 0.75, 1];

  return (
    <Box sx={{ width: '100%', overflow: 'hidden' }}>
      <Box
        component="svg"
        viewBox={`0 0 ${width} ${height}`}
        sx={{ width: '100%', height: 'auto' }}
      >
        {ticks.map((ratio) => {
          const y = padding.top + plotHeight * (1 - ratio);
          const value = Math.round(max * ratio);

          return (
            <g key={ratio}>
              <line
                x1={padding.left}
                x2={width - padding.right}
                y1={y}
                y2={y}
                stroke={palette.divider}
                strokeWidth={1}
              />
              <text
                x={padding.left - 8}
                y={y + 4}
                textAnchor="end"
                fontSize={11}
                fill={palette.text.secondary}
              >
                {compactNumber(value)}
              </text>
            </g>
          );
        })}

        {buckets.map((bucket, index) => {
          const total = totals[index];
          const full = (total / max) * plotHeight;
          const completion = (bucket.completion_tokens / max) * plotHeight;
          const prompt = full - completion;
          const x = padding.left + slot * index + (slot - barWidth) / 2;
          const baseY = padding.top + plotHeight;

          return (
            <g key={bucket.key}>
              <title>
                {`${bucket.key}：${bucket.requests} 次 · ${compactNumber(total)} token`}
              </title>

              <rect
                x={x}
                y={baseY - full}
                width={barWidth}
                height={Math.max(prompt, 0)}
                rx={2}
                fill={palette.primary.main}
              />
              <rect
                x={x}
                y={baseY - completion}
                width={barWidth}
                height={Math.max(completion, 0)}
                rx={2}
                fill={palette.info.main}
              />

              <text
                x={x + barWidth / 2}
                y={height - padding.bottom + 16}
                textAnchor="middle"
                fontSize={11}
                fill={palette.text.secondary}
              >
                {shortKey(bucket.key)}
              </text>
            </g>
          );
        })}

        <line
          x1={padding.left}
          x2={width - padding.right}
          y1={padding.top + plotHeight}
          y2={padding.top + plotHeight}
          stroke={palette.divider}
        />
      </Box>

      <Stack direction="row" spacing={2} sx={{ justifyContent: 'flex-end', mt: 0.5 }}>
        <Legend color={palette.primary.main} label="prompt" />
        <Legend color={palette.info.main} label="completion" />
      </Stack>
    </Box>
  );
}

function Legend({ color, label }: { color: string; label: string }) {
  return (
    <Stack direction="row" spacing={0.5} sx={{ alignItems: 'center' }}>
      <Box sx={{ width: 10, height: 10, borderRadius: 0.5, bgcolor: color }} />
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        {label}
      </Typography>
    </Stack>
  );
}

function TopList({ buckets, total }: { buckets: UsageBucket[]; total: number }) {
  const top = [...buckets]
    .map((bucket) => ({ ...bucket, tokens: bucket.prompt_tokens + bucket.completion_tokens }))
    .sort((a, b) => b.tokens - a.tokens)
    .slice(0, 5);

  return (
    <Stack spacing={1.5}>
      {top.map((bucket) => {
        const share = total > 0 ? (bucket.tokens / total) * 100 : 0;

        return (
          <Box key={bucket.key}>
            <Stack direction="row" spacing={1} sx={{ alignItems: 'baseline', mb: 0.5 }}>
              <Typography variant="body2" noWrap sx={{ flex: '1 1 auto', minWidth: 0 }}>
                {bucket.key}
              </Typography>
              <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                {share.toFixed(1)}%
              </Typography>
            </Stack>
            <LinearProgress
              variant="determinate"
              value={Math.min(share, 100)}
              sx={{ height: 6, borderRadius: 3 }}
            />
          </Box>
        );
      })}
    </Stack>
  );
}

function BucketRow({ bucket, total }: { bucket: UsageBucket; total: number }) {
  const tokens = bucket.prompt_tokens + bucket.completion_tokens;
  const share = total > 0 ? (tokens / total) * 100 : 0;
  const avgLatency = bucket.requests > 0 ? Math.round(bucket.latency_ms / bucket.requests) : 0;

  return (
    <TableRow hover>
      <TableCell sx={{ fontFamily: 'monospace' }}>{bucket.key}</TableCell>
      <TableCell align="right">{bucket.requests.toLocaleString('zh-CN')}</TableCell>
      <TableCell align="right">{compactNumber(bucket.prompt_tokens)}</TableCell>
      <TableCell align="right">{compactNumber(bucket.completion_tokens)}</TableCell>
      <TableCell align="right">{compactNumber(tokens)}</TableCell>
      <TableCell align="right">{formatLatency(avgLatency)}</TableCell>
      <TableCell>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <LinearProgress
            variant="determinate"
            value={Math.min(share, 100)}
            sx={{ flex: '1 1 auto', height: 6, borderRadius: 3 }}
          />
          <Typography
            variant="caption"
            sx={{ color: 'text.secondary', width: 44, textAlign: 'right' }}
          >
            {share.toFixed(1)}%
          </Typography>
        </Stack>
      </TableCell>
    </TableRow>
  );
}

function StatCard({ label, value, hint }: { label: string; value: string; hint?: string }) {
  return (
    <Card variant="outlined" sx={{ flex: '1 1 160px' }}>
      <CardContent>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {label}
        </Typography>
        <Typography variant="h5" sx={{ mt: 0.5 }}>
          {value}
        </Typography>
        {hint && (
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
            {hint}
          </Typography>
        )}
      </CardContent>
    </Card>
  );
}

// ----------------------------------------------------------------------

/**
 * 按天补零：把 `[from, to]` 里缺失的日期补成 0 请求的桶。
 *
 * 后端的分组是 `GROUP BY`，没有行的日期不会出现；图表上就少一根柱子，看起来像那几天
 * 不存在。补零后横轴才是连续时间线（表格里也会显示 0，符合「区间内没有数据」的口径）。
 */
function fillDays(buckets: UsageBucket[], from: string, to: string): UsageBucket[] {
  const existing = new Map(buckets.map((bucket) => [bucket.key, bucket]));
  const filled: UsageBucket[] = [];
  const cursor = new Date(`${from}T00:00:00`);
  const end = new Date(`${to}T00:00:00`);

  while (cursor <= end) {
    const key = asDateInput(cursor);
    filled.push(
      existing.get(key) ?? {
        key,
        requests: 0,
        prompt_tokens: 0,
        completion_tokens: 0,
        latency_ms: 0,
      }
    );
    cursor.setDate(cursor.getDate() + 1);
  }

  return filled;
}

/** 大数字压缩：token 动辄百万，原样列出读不出量级。 */
function compactNumber(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 10_000) return `${(value / 10_000).toFixed(1)} 万`;

  return Math.round(value).toLocaleString('zh-CN');
}

/** 耗时：< 1s 用 ms，否则用秒（保留一位小数）。 */
function formatLatency(ms: number): string {
  if (ms <= 0) return '—';
  if (ms < 1000) return `${ms} ms`;

  return `${(ms / 1000).toFixed(1)} s`;
}

/** 图表横轴标签：日期取 `MM-DD`，其它维度截断到 8 个字符。 */
function shortKey(key: string): string {
  if (/^\d{4}-\d{2}-\d{2}$/.test(key)) return key.slice(5);

  return key.length > 8 ? `${key.slice(0, 7)}…` : key;
}

// ----------------------------------------------------------------------

/** 默认区间：最近 7 天（含今天），与后端缺省一致。 */
function defaultRange(): { from: string; to: string } {
  const today = new Date();
  const start = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 6);

  return { from: asDateInput(start), to: asDateInput(today) };
}

/** `YYYY-MM-DD`（按本地时区，与后端的本地日期口径一致）。 */
function asDateInput(value: Date): string {
  const month = `${value.getMonth() + 1}`.padStart(2, '0');
  const day = `${value.getDate()}`.padStart(2, '0');

  return `${value.getFullYear()}-${month}-${day}`;
}
