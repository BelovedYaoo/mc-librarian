import type { UsageBucket, UsageGroupBy, UsageReport } from 'src/lib/api-client';

import { useState } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import CircularProgress from '@mui/material/CircularProgress';
import Divider from '@mui/material/Divider';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';

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

/**
 * `/usage`：管理员查看用量（后端 `GET /v1/admin/usage`）。
 *
 * 顶部卡片的四个数字与下方表格全部来自同一个响应（服务端算好的 `totals`/`buckets`），
 * 前端不再自己累加——聚合口径只有一份。未配单价时成本恒为 0，必须显式标注，
 * 否则会被读成「这段时间没花钱」。
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
  return (
    <Stack spacing={3}>
      <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
        <StatCard label="请求数" value={report.requests.toLocaleString('zh-CN')} />
        <StatCard
          label="prompt tokens"
          value={(report.totals.prompt_tokens ?? 0).toLocaleString('zh-CN')}
        />
        <StatCard
          label="completion tokens"
          value={(report.totals.completion_tokens ?? 0).toLocaleString('zh-CN')}
        />
        <StatCard label="估算成本" value={formatCost(report.totals.cost_usd ?? 0)} />
      </Stack>

      {!report.cost_configured && (
        <Alert severity="warning">
          未配置单价，仅统计 token：成本恒为 0，不代表真的没花钱。配置
          MC_LIBRARIAN_LLM_INPUT_PRICE_PER_M / MC_LIBRARIAN_LLM_OUTPUT_PRICE_PER_M 后重新查看。
        </Alert>
      )}

      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        区间 {report.from} → {report.to}（含当天）
      </Typography>

      {report.buckets.length === 0 ? (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          这段时间没有用量数据。
        </Typography>
      ) : (
        <Box sx={{ overflowX: 'auto' }}>
          <Table size="small">
            <TableHead>
              <TableRow>
                <TableCell>{GROUP_LABELS[groupBy]}</TableCell>
                <TableCell align="right">请求数</TableCell>
                <TableCell align="right">prompt tokens</TableCell>
                <TableCell align="right">completion tokens</TableCell>
                <TableCell align="right">成本</TableCell>
              </TableRow>
            </TableHead>

            <TableBody>
              {report.buckets.map((bucket) => (
                <BucketRow key={bucket.key} bucket={bucket} />
              ))}
            </TableBody>
          </Table>
        </Box>
      )}
    </Stack>
  );
}

function BucketRow({ bucket }: { bucket: UsageBucket }) {
  return (
    <TableRow>
      <TableCell sx={{ fontFamily: 'monospace' }}>{bucket.key}</TableCell>
      <TableCell align="right">{bucket.requests.toLocaleString('zh-CN')}</TableCell>
      <TableCell align="right">{bucket.prompt_tokens.toLocaleString('zh-CN')}</TableCell>
      <TableCell align="right">{bucket.completion_tokens.toLocaleString('zh-CN')}</TableCell>
      <TableCell align="right">{formatCost(bucket.cost_usd)}</TableCell>
    </TableRow>
  );
}

function StatCard({ label, value }: { label: string; value: string }) {
  return (
    <Card sx={{ flex: '1 1 160px' }}>
      <CardContent>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {label}
        </Typography>
        <Typography variant="h5" sx={{ mt: 0.5 }}>
          {value}
        </Typography>
      </CardContent>
    </Card>
  );
}

// ----------------------------------------------------------------------

/** 成本口径是「未配单价时记 0」（后端 `cost_usd`）：数字照接口的显示，口径由上面的告警标注。 */
function formatCost(value: number): string {
  return `$${value.toFixed(4)}`;
}

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
