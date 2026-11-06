import type { AuditEntry } from 'src/lib/api-client';

import { useEffect, useState } from 'react';
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
import MenuItem from '@mui/material/MenuItem';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';

import { useAudit } from 'src/hooks';
import { listUsers } from 'src/lib/api-client';

// ----------------------------------------------------------------------

/**
 * `/audit`：管理员按人筛选查看敏感操作流水（后端 `GET /v1/admin/audit`）。
 * 「筛选」提交后才发起请求，避免每敲一个字就打一次接口。
 */
export function AuditView() {
  const [draft, setDraft] = useState('');
  const [actor, setActor] = useState('');
  const { entries, loading, error, refresh } = useAudit(actor);

  // 操作者下拉的选项：直接取用户列表（本页仅 admin 可达，接口本身也要求 admin）。
  const [actors, setActors] = useState<string[]>([]);

  useEffect(() => {
    let cancelled = false;

    void listUsers()
      .then((data) => {
        if (!cancelled) setActors((data.users ?? []).map((user) => user.username));
      })
      .catch(() => {
        // 取不到就退化成空列表：筛选框只剩「全部」，不影响主流程。
      });

    return () => {
      cancelled = true;
    };
  }, []);

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

      {/* 审计是长列表：卡片给一个随视口收缩的高度上限，滚动条留在表格里，
          而不是把整页撑长（整页滚动时表头和筛选条会跟着滚走）。 */}
      <Card sx={{ height: 'calc(100dvh - 260px)', minHeight: 360, display: 'flex' }}>
        <CardContent
          sx={{ display: 'flex', flexDirection: 'column', flex: '1 1 auto', minHeight: 0, width: '100%' }}
        >
          <Stack
            component="form"
            direction={{ xs: 'column', sm: 'row' }}
            spacing={2}
            onSubmit={handleFilter}
            noValidate
            sx={{ alignItems: { sm: 'center' } }}
          >
            {/* 操作者是「从已知用户里挑一个」，不是自由输入：输入框既容易打错，
                也筛不出「这个人不存在」这种有用信息。 */}
            <TextField
              select
              label="操作者"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
              size="small"
              sx={{ minWidth: 220 }}
            >
              <MenuItem value="">全部</MenuItem>
              {actors.map((name) => (
                <MenuItem key={name} value={name}>
                  {name}
                </MenuItem>
              ))}
            </TextField>

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
            <Box sx={{ flex: '1 1 auto', minHeight: 0, overflow: 'auto' }}>
              <Table size="small" stickyHeader>
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
