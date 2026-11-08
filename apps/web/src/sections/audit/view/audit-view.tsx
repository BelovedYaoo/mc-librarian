import type { AuditEntry } from 'src/lib/api-client';

import { useEffect, useState } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
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

import { formatTime } from 'src/utils/format-time';

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
      <Card sx={{ height: 'calc(100dvh - 292px)', minHeight: 360, display: 'flex' }}>
        <CardContent
          sx={{
            display: 'flex',
            flexDirection: 'column',
            flex: '1 1 auto',
            minHeight: 0,
            width: '100%',
          }}
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
                      <TableCell>
                        <Chip
                          size="small"
                          variant="outlined"
                          color={ACTION_META[entry.action]?.color ?? 'default'}
                          label={ACTION_META[entry.action]?.label ?? entry.action}
                          title={entry.action}
                        />
                      </TableCell>
                      <TableCell>{entry.target ?? '—'}</TableCell>
                      <TableCell sx={{ wordBreak: 'break-word' }}>{formatDetail(entry)}</TableCell>
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

/**
 * 动作名 → 人话标签 + 颜色。
 *
 * `action` 是稳定的机器键（`version.registered`），直接摆在表格里对管理员没有意义：
 * 这列是给人扫的，所以显示中文标签，机器键留在 `title` 里（悬停可见），排障时仍能查到。
 */
const ACTION_META: Record<
  string,
  { label: string; color: 'success' | 'error' | 'warning' | 'default' }
> = {
  'user.created': { label: '创建用户', color: 'success' },
  'user.role_changed': { label: '修改角色', color: 'warning' },
  'user.disabled': { label: '停用用户', color: 'error' },
  'user.enabled': { label: '启用用户', color: 'success' },
  'user.deleted': { label: '删除用户', color: 'error' },
  'invite.created': { label: '创建邀请', color: 'success' },
  'invite.accepted': { label: '接受邀请', color: 'default' },
  'token.created': { label: '创建令牌', color: 'success' },
  'token.revoked': { label: '吊销令牌', color: 'error' },
  'job.created': { label: '创建任务', color: 'default' },
  'job.canceled': { label: '取消任务', color: 'error' },
  'job.retried': { label: '重试任务', color: 'warning' },
  'version.registered': { label: '注册版本', color: 'success' },
  'version.recorded': { label: '记录版本', color: 'default' },
  'settings.updated': { label: '修改设置', color: 'warning' },
};

/** 大数字压缩：1,234 / 12.3 万 / 6.86M（token 动辄百万，原样列出没人读得出来）。 */
function compactNumber(value: number): string {
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(2)}M`;
  if (value >= 10_000) return `${(value / 10_000).toFixed(1)} 万`;

  return value.toLocaleString('zh-CN');
}

/**
 * `detail` 是自由 JSON（各动作字段不同）。按动作翻译成人话，认不出的键值退回 `键 值` 拼接。
 */
function formatDetail(entry: AuditEntry): string {
  const detail = (entry.detail ?? {}) as Record<string, unknown>;
  const keys = Object.keys(detail);

  if (keys.length === 0) return '—';

  switch (entry.action) {
    case 'version.registered':
    case 'version.recorded': {
      const parts = [
        detail.files != null ? `${compactNumber(Number(detail.files))} 个文件` : null,
        detail.tokens != null ? `${compactNumber(Number(detail.tokens))} token` : null,
        detail.obfuscated ? '混淆' : null,
        detail.has_summaries ? '含摘要' : null,
      ].filter(Boolean);

      return parts.length > 0 ? parts.join(' · ') : '—';
    }
    case 'settings.updated': {
      const changed = Array.isArray(detail.keys) ? detail.keys : [];

      return changed.length > 0 ? `修改 ${changed.join('、')}` : '—';
    }
    case 'invite.created':
      return detail.expires_in_hours != null ? `有效期 ${detail.expires_in_hours} 小时` : '—';
    case 'token.created': {
      const parts = [
        detail.name != null ? `名称 ${String(detail.name)}` : null,
        detail.expires_in_days != null ? `有效期 ${detail.expires_in_days} 天` : null,
      ].filter(Boolean);

      return parts.length > 0 ? parts.join(' · ') : '—';
    }
    case 'user.created':
    case 'user.deleted':
      return detail.role != null ? `角色 ${detail.role}` : '—';
    case 'user.disabled':
      return detail.sessions_revoked != null ? `撤销 ${detail.sessions_revoked} 个会话` : '—';
    case 'user.role_changed':
      return detail.from != null && detail.to != null ? `${detail.from} → ${detail.to}` : '—';
    case 'job.created':
      return [detail.kind, detail.version].filter(Boolean).join(' · ') || '—';
    case 'job.canceled':
    case 'job.retried':
      return detail.from_status != null ? `原状态 ${detail.from_status}` : '—';
    default:
      return keys.map((key) => `${key} ${String(detail[key])}`).join(' · ');
  }
}
