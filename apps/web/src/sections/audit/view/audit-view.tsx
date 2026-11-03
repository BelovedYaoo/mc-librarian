import type { AuditEntry } from 'src/lib/api-client';

import { useState } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import CircularProgress from '@mui/material/CircularProgress';
import Divider from '@mui/material/Divider';
import Stack from '@mui/material/Stack';
import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';

import { useAudit } from 'src/hooks';

// ----------------------------------------------------------------------

/**
 * `/audit`：管理员按人筛选查看敏感操作流水（后端 `GET /v1/admin/audit`）。
 * 「筛选」提交后才发起请求，避免每敲一个字就打一次接口。
 */
export function AuditView() {
  const [draft, setDraft] = useState('');
  const [actor, setActor] = useState('');
  const { entries, loading, error, refresh } = useAudit(actor);

  const handleFilter = (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setActor(draft.trim());
  };

  const handleReset = () => {
    setDraft('');
    setActor('');
  };

  return (
    <Stack spacing={3}>
      <div>
        <Typography variant="h4" sx={{ mb: 1 }}>
          审计
        </Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          谁在什么时候对什么做了什么。按操作者（用户名）精确筛选，最新的在前。
        </Typography>
      </div>

      <Card variant="outlined">
        <CardContent>
          <Stack
            component="form"
            direction={{ xs: 'column', sm: 'row' }}
            spacing={2}
            onSubmit={handleFilter}
            noValidate
            sx={{ alignItems: { sm: 'center' } }}
          >
            <TextField
              label="操作者"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              placeholder="用户名，例如 admin"
              size="small"
              sx={{ minWidth: 220 }}
            />

            <Button type="submit" size="small" variant="contained">
              筛选
            </Button>

            <Button size="small" variant="outlined" onClick={handleReset}>
              重置
            </Button>

            <Box sx={{ flex: '1 1 auto' }} />

            {loading && <CircularProgress size={16} />}

            <Button size="small" variant="outlined" onClick={refresh}>
              刷新
            </Button>
          </Stack>

          <Divider sx={{ my: 2 }} />

          {error && <Alert severity="error">读取审计失败：{error}</Alert>}

          {!error && entries.length === 0 && !loading && (
            <Typography variant="body2" sx={{ color: 'text.secondary' }}>
              {actor ? `没有操作者为 ${actor} 的记录。` : '还没有审计记录。'}
            </Typography>
          )}

          {entries.length > 0 && (
            <Box sx={{ overflowX: 'auto' }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>时间</TableCell>
                    <TableCell>操作者</TableCell>
                    <TableCell>动作</TableCell>
                    <TableCell>目标</TableCell>
                    <TableCell>详情</TableCell>
                    <TableCell>IP</TableCell>
                  </TableRow>
                </TableHead>

                <TableBody>
                  {entries.map((entry) => (
                    <TableRow key={entry.id}>
                      <TableCell sx={{ whiteSpace: 'nowrap' }}>
                        {formatTime(entry.created_at)}
                      </TableCell>
                      <TableCell>{entry.actor}</TableCell>
                      <TableCell sx={{ fontFamily: 'monospace' }}>{entry.action}</TableCell>
                      <TableCell>{entry.target ?? '—'}</TableCell>
                      <TableCell sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>
                        {formatDetail(entry)}
                      </TableCell>
                      <TableCell>{entry.ip ?? '—'}</TableCell>
                    </TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          )}
        </CardContent>
      </Card>
    </Stack>
  );
}

// ----------------------------------------------------------------------

/** `detail` 是自由 JSON；空对象（后端对无详情动作写 `{}`）显示成 `—`。 */
function formatDetail(entry: AuditEntry): string {
  const keys = Object.keys(entry.detail ?? {});

  return keys.length === 0 ? '—' : JSON.stringify(entry.detail);
}

function formatTime(value: string): string {
  const parsed = new Date(value);

  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN');
}
