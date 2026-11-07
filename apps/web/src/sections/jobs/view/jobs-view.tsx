import type { Job, JobLog, JobStage, JobStatus } from 'src/lib/api-client';

import { useState } from 'react';
import { useSearchParams } from 'react-router';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import Divider from '@mui/material/Divider';
import LinearProgress from '@mui/material/LinearProgress';
import List from '@mui/material/List';
import ListItemButton from '@mui/material/ListItemButton';
import ListItemText from '@mui/material/ListItemText';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';

import { useJobLogs, useJobs } from 'src/hooks';

import { ApiError, cancelJob, retryJob } from 'src/lib/api-client';

import { formatTime } from 'src/utils/format-time';

// ----------------------------------------------------------------------

// 阶段枚举与后端 `Job.stage` 一致（五段）：`#4` 的带源目录任务跑后三段，
// `#21` 的不带源目录任务跑前两段。标签与顺序只在这一处定义，指示器与列表的「当前阶段」共用。
const STAGE_LABELS: Record<JobStage, string> = {
  download: '下载',
  decompile: '反编译',
  parse: '解析',
  summarize: '摘要',
  finalize: '收尾',
};

const STAGES = Object.keys(STAGE_LABELS) as JobStage[];

type ChipColor = 'default' | 'primary' | 'success' | 'error' | 'warning' | 'info';

const STATUS_META: Record<JobStatus, { label: string; color: ChipColor }> = {
  queued: { label: '排队中', color: 'warning' },
  running: { label: '运行中', color: 'primary' },
  succeeded: { label: '已完成', color: 'success' },
  failed: { label: '失败', color: 'error' },
  canceled: { label: '已取消', color: 'default' },
};

// ----------------------------------------------------------------------

/** 任务列表 + 进度 + 日志面板 + 取消按钮（仅 admin 可达，路由层有 RequireAdmin）。 */
export function JobsView() {
  const { jobs, error, refresh } = useJobs();
  const [searchParams, setSearchParams] = useSearchParams();
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyAction, setBusyAction] = useState<'cancel' | 'retry' | null>(null);

  // 选中任务以 `?job=<id>` 为准：刷新与分享链接都能回到同一个任务。
  // 未知 id 回落到列表首个，列表为空则回落到空态。
  const selectedId = searchParams.get('job');
  const selected = jobs.find((job) => job.id === selectedId) ?? jobs[0] ?? null;
  const { lines, error: logsError } = useJobLogs(selected?.id ?? null);

  const handleCancel = async () => {
    if (!selected) return;

    setBusyAction('cancel');
    setActionError(null);

    try {
      await cancelJob(selected.id);
      refresh();
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setBusyAction(null);
    }
  };

  const handleRetry = async () => {
    if (!selected) return;

    setBusyAction('retry');
    setActionError(null);

    try {
      await retryJob(selected.id);
      refresh();
    } catch (cause) {
      setActionError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setBusyAction(null);
    }
  };

  return (
    <Stack spacing={3}>
      <Stack direction="row" spacing={2} sx={{ alignItems: 'center' }}>
        <Typography variant="h4">任务</Typography>
        <Box sx={{ flex: '1 1 auto' }} />

        <Button size="small" variant="outlined" onClick={refresh}>
          刷新
        </Button>
      </Stack>

      {error && <Alert severity="error">读取任务列表失败：{error}</Alert>}

      <Stack direction={{ xs: 'column', md: 'row' }} spacing={3} sx={{ alignItems: 'flex-start' }}>
        <Card sx={{ width: { xs: '100%', md: 280 }, flex: '0 0 auto' }}>
          <CardContent>
            <Typography variant="h6" sx={{ mb: 1 }}>
              任务队列
            </Typography>

            <Divider sx={{ mb: 1 }} />

            {jobs.length === 0 ? (
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                还没有任务。
              </Typography>
            ) : (
              <List dense disablePadding>
                {jobs.map((job) => (
                  <ListItemButton
                    key={job.id}
                    selected={job.id === selected?.id}
                    onClick={() => setSearchParams({ job: job.id })}
                    sx={{ borderRadius: 1 }}
                  >
                    <ListItemText
                      primary={`${job.version ?? '—'} · ${STATUS_META[job.status].label}`}
                      secondary={job.stage ? STAGE_LABELS[job.stage] : '等待调度'}
                    />
                  </ListItemButton>
                ))}
              </List>
            )}
          </CardContent>
        </Card>

        <Stack spacing={3} sx={{ flex: '1 1 auto', minWidth: 0, width: '100%' }}>
          {selected ? (
            <>
              <JobDetail
                job={selected}
                busyAction={busyAction}
                actionError={actionError}
                onCancel={handleCancel}
                onRetry={handleRetry}
              />
              <LogPanel lines={lines} error={logsError} />
            </>
          ) : (
            <Card>
              <CardContent>
                <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                  选中左侧任务查看进度与日志。
                </Typography>
              </CardContent>
            </Card>
          )}
        </Stack>
      </Stack>
    </Stack>
  );
}

// ----------------------------------------------------------------------

type JobDetailProps = {
  job: Job;
  busyAction: 'cancel' | 'retry' | null;
  actionError: string | null;
  onCancel: () => void;
  onRetry: () => void;
};

function JobDetail({ job, busyAction, actionError, onCancel, onRetry }: JobDetailProps) {
  const meta = STATUS_META[job.status];
  const cancellable = job.status === 'queued' || job.status === 'running';
  // 只有 failed/canceled 能重试：后端保留 checkpoint，从下一个未完成阶段续跑。
  const retryable = job.status === 'failed' || job.status === 'canceled';

  return (
    <Card>
      <CardContent>
        <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center', mb: 2 }}>
          <Typography variant="h6">{job.version ?? '未指定版本'}</Typography>
          <Chip size="small" color={meta.color} label={meta.label} />
          <Box sx={{ flex: '1 1 auto' }} />

          <Button
            size="small"
            variant="outlined"
            disabled={!retryable || busyAction !== null}
            onClick={onRetry}
          >
            {busyAction === 'retry' ? '重试中…' : '重试'}
          </Button>

          <Button
            size="small"
            color="error"
            variant="outlined"
            disabled={!cancellable || busyAction !== null}
            onClick={onCancel}
          >
            {busyAction === 'cancel' ? '取消中…' : '取消任务'}
          </Button>
        </Stack>

        <LinearProgress variant="determinate" value={Math.round((job.progress ?? 0) * 100)} />

        <Stack direction="row" spacing={1} sx={{ mt: 2, flexWrap: 'wrap' }}>
          {STAGES.map((stage) => (
            <Chip
              key={stage}
              size="small"
              variant={job.stage === stage ? 'filled' : 'outlined'}
              color={job.stage === stage ? 'primary' : 'default'}
              label={STAGE_LABELS[stage]}
            />
          ))}
        </Stack>

        <Stack spacing={1} sx={{ mt: 2 }}>
          <Row label="任务 ID" value={job.id} mono />
          <Row label="当前阶段" value={job.stage ? STAGE_LABELS[job.stage] : '等待调度'} />
          <Row label="状态说明" value={job.message ?? '—'} />
          <Row label="错误" value={job.error ?? '—'} />
          <Row label="创建时间" value={formatTime(job.created_at)} />
          <Row label="结束时间" value={job.finished_at ? formatTime(job.finished_at) : '—'} />
        </Stack>

        {actionError && (
          <Alert severity="error" sx={{ mt: 2 }}>
            {actionError}
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}

// ----------------------------------------------------------------------

type LogPanelProps = {
  lines: JobLog[];
  error: string | null;
};

function LogPanel({ lines, error }: LogPanelProps) {
  return (
    <Card>
      <CardContent>
        <Typography variant="h6" sx={{ mb: 1 }}>
          日志
        </Typography>

        <Divider sx={{ mb: 1 }} />

        {error && <Alert severity="error">{error}</Alert>}

        <Box
          sx={{
            mt: 1,
            p: 2,
            borderRadius: 1,
            bgcolor: 'grey.900',
            color: 'grey.100',
            fontFamily: 'monospace',
            fontSize: 12,
            maxHeight: 360,
            overflow: 'auto',
          }}
        >
          {lines.length === 0 ? (
            <Typography variant="caption" sx={{ color: 'grey.500' }}>
              暂无日志
            </Typography>
          ) : (
            lines.map((line) => (
              <div key={line.id}>
                {formatTime(line.ts)} [{line.level}] {line.message}
              </div>
            ))
          )}
        </Box>
      </CardContent>
    </Card>
  );
}

// ----------------------------------------------------------------------

type RowProps = {
  label: string;
  value: string;
  mono?: boolean;
};

function Row({ label, value, mono = false }: RowProps) {
  return (
    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={{ xs: 0.5, sm: 2 }}>
      <Typography variant="body2" sx={{ color: 'text.secondary', minWidth: 80 }}>
        {label}
      </Typography>
      <Typography
        variant="body2"
        sx={{ fontFamily: mono ? 'monospace' : undefined, wordBreak: 'break-all' }}
      >
        {value}
      </Typography>
    </Stack>
  );
}
