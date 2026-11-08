import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogTitle from '@mui/material/DialogTitle';
import Divider from '@mui/material/Divider';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import Switch from '@mui/material/Switch';
import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';
import TextField from '@mui/material/TextField';
import Tooltip from '@mui/material/Tooltip';
import Typography from '@mui/material/Typography';
import { useCallback, useEffect, useState } from 'react';

import type { CreatedUser, PatchUserRequest, User } from 'src/lib/api-client';

import { useAuth } from 'src/auth';

import { formatTime } from 'src/utils/format-time';
import { createUser, deleteUser, listUsers, patchUser } from 'src/lib/api-client';

// ----------------------------------------------------------------------

type Role = User['role'];

type UsersState =
  { status: 'loading' } | { status: 'ready'; users: User[] } | { status: 'error'; message: string };

function messageOf(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught);
}

/** 后端返回的是站点根下的相对路径；这里补成可以直接发给别人的绝对链接。 */
function absoluteInviteUrl(path: string): string {
  return `${window.location.origin}${path}`;
}

/**
 * `/users`：管理员创建用户（一次性邀请链接）、改角色、启停。
 * 「最后一个启用的 admin 不能降级/停用」由后端 409 兜底，这里只负责把提示显示出来。
 */
export function UsersView() {
  const { user: currentUser } = useAuth();

  const [state, setState] = useState<UsersState>({ status: 'loading' });
  const [actionError, setActionError] = useState<string | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);

  const [username, setUsername] = useState('');
  const [role, setRole] = useState<Role>('user');
  const [creating, setCreating] = useState(false);
  const [invite, setInvite] = useState<CreatedUser | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async (signal?: AbortSignal) => {
    try {
      const data = await listUsers(signal);

      setState({ status: 'ready', users: data.users ?? [] });
    } catch (caught) {
      if (signal?.aborted) return;

      setState({ status: 'error', message: messageOf(caught) });
    }
  }, []);

  useEffect(() => {
    const controller = new AbortController();

    void load(controller.signal);

    return () => controller.abort();
  }, [load]);

  const handlePatch = async (target: User, body: PatchUserRequest) => {
    setBusyId(target.id);
    setActionError(null);

    try {
      const updated = await patchUser(target.id, body);

      setState((current) =>
        current.status === 'ready'
          ? {
              status: 'ready',
              users: current.users.map((item) => (item.id === updated.id ? updated : item)),
            }
          : current
      );
    } catch (caught) {
      setActionError(messageOf(caught));
    } finally {
      setBusyId(null);
    }
  };

  const [pendingDelete, setPendingDelete] = useState<User | null>(null);
  const [deleting, setDeleting] = useState(false);

  const handleDelete = async () => {
    if (pendingDelete === null) return;

    setDeleting(true);
    setActionError(null);

    try {
      await deleteUser(pendingDelete.id);
      setPendingDelete(null);
      await load();
    } catch (caught) {
      setActionError(messageOf(caught));
    } finally {
      setDeleting(false);
    }
  };

  const handleCreate = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setCreating(true);
    setActionError(null);
    setCopied(false);

    try {
      const created = await createUser({ username: username.trim(), role });

      setInvite(created);
      setUsername('');
      await load();
    } catch (caught) {
      setActionError(messageOf(caught));
    } finally {
      setCreating(false);
    }
  };

  const handleCopy = async () => {
    if (!invite) return;

    try {
      await navigator.clipboard.writeText(absoluteInviteUrl(invite.invite_url));
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <Stack spacing={3}>
      <div>
        <Typography variant="h4" sx={{ mb: 1 }}>
          用户管理
        </Typography>
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          创建账号、调整角色、启用或禁用。密码由被邀请人自设，管理员全程不接触。
        </Typography>
      </div>

      {actionError && <Alert severity="error">{actionError}</Alert>}

      {invite && (
        <Alert severity="success" onClose={() => setInvite(null)}>
          <Typography variant="body2">
            已创建 <b>{invite.user.username}</b>
            ，把这条一次性链接发给他：48 小时内有效，且只能用一次。
          </Typography>

          <Stack
            direction={{ xs: 'column', sm: 'row' }}
            spacing={1}
            sx={{ mt: 1.5, alignItems: { sm: 'center' } }}
          >
            <Typography
              variant="body2"
              sx={{ fontFamily: 'monospace', wordBreak: 'break-all', flex: '1 1 auto' }}
            >
              {absoluteInviteUrl(invite.invite_url)}
            </Typography>

            <Button size="small" variant="outlined" onClick={handleCopy} sx={{ flex: '0 0 auto' }}>
              {copied ? '已复制' : '复制链接'}
            </Button>
          </Stack>
        </Alert>
      )}

      <Card>
        <CardContent>
          <Typography variant="h6" sx={{ mb: 2 }}>
            创建用户
          </Typography>

          <Divider sx={{ mb: 2 }} />

          <Stack component="form" spacing={2} onSubmit={handleCreate} noValidate>
            <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
              <TextField
                label="用户名"
                value={username}
                onChange={(event) => setUsername(event.target.value)}
                helperText="3–32 位，允许字母、数字、下划线、点和短横线"
                fullWidth
                required
              />

              <TextField
                select
                label="角色"
                value={role}
                onChange={(event) => setRole(event.target.value as Role)}
                sx={{ minWidth: 140 }}
              >
                <MenuItem value="user">普通用户</MenuItem>
                <MenuItem value="admin">管理员</MenuItem>
              </TextField>

              <Button
                type="submit"
                variant="contained"
                disabled={creating || username.trim().length === 0}
                sx={{ whiteSpace: 'nowrap', height: 56 }}
              >
                {creating ? '创建中…' : '创建'}
              </Button>
            </Stack>
          </Stack>
        </CardContent>
      </Card>

      <Card>
        <CardContent>
          <Stack direction="row" spacing={2} sx={{ alignItems: 'center', mb: 2 }}>
            <Typography variant="h6">全部用户</Typography>
            <Box sx={{ flex: '1 1 auto' }} />
            {state.status === 'loading' && <CircularProgress size={16} />}
            <Button size="small" variant="outlined" onClick={() => void load()}>
              刷新
            </Button>
          </Stack>

          <Divider />

          {state.status === 'error' && (
            <Alert severity="error" sx={{ mt: 2 }}>
              读取用户列表失败：{state.message}
            </Alert>
          )}

          {state.status === 'ready' && state.users.length === 0 && (
            <Typography variant="body2" sx={{ color: 'text.secondary', mt: 2 }}>
              还没有用户。
            </Typography>
          )}

          {state.status === 'ready' && state.users.length > 0 && (
            <Box sx={{ overflowX: 'auto', mt: 1 }}>
              <Table size="small">
                <TableHead>
                  <TableRow>
                    <TableCell>用户</TableCell>
                    <TableCell>角色</TableCell>
                    <TableCell>状态</TableCell>
                    <TableCell>创建时间</TableCell>
                    <TableCell>最近登录</TableCell>
                    <TableCell align="right">操作</TableCell>
                  </TableRow>
                </TableHead>

                <TableBody>
                  {state.users.map((item) => (
                    <UserTableRow
                      key={item.id}
                      user={item}
                      isCurrent={item.id === currentUser?.id}
                      busy={busyId === item.id}
                      // 最后一个启用中的管理员既不能停用也不能降级，前端先说清楚，
                      // 别让开关弹回来还只在页面顶部挂一行错误。
                      isLastAdmin={
                        item.role === 'admin' &&
                        item.enabled &&
                        state.users.filter((u) => u.role === 'admin' && u.enabled).length <= 1
                      }
                      onPatch={(body) => void handlePatch(item, body)}
                      onDelete={() => setPendingDelete(item)}
                    />
                  ))}
                </TableBody>
              </Table>
            </Box>
          )}
        </CardContent>
      </Card>

      {/* 删除是破坏性动作：二次确认，并把「会一起删掉什么」写清楚。 */}
      <Dialog open={pendingDelete !== null} onClose={() => setPendingDelete(null)}>
        <DialogTitle>删除用户 {pendingDelete?.username}？</DialogTitle>
        <DialogContent>
          <Typography variant="body2">
            该用户的会话、访问令牌、问答历史会一并删除；审计日志与用量记录保留（引用置为空）。
            此操作不可撤销。
          </Typography>
        </DialogContent>
        <DialogActions>
          <Button onClick={() => setPendingDelete(null)} disabled={deleting}>
            取消
          </Button>
          <Button
            color="error"
            variant="contained"
            onClick={() => void handleDelete()}
            disabled={deleting}
          >
            {deleting ? '删除中…' : '删除'}
          </Button>
        </DialogActions>
      </Dialog>
    </Stack>
  );
}

// ----------------------------------------------------------------------

type UserRowProps = {
  user: User;
  isCurrent: boolean;
  busy: boolean;
  /** 系统里唯一一个启用中的管理员：停用与降级都会被后端 409 拒绝。 */
  isLastAdmin: boolean;
  onPatch: (body: PatchUserRequest) => void;
  onDelete: () => void;
};

function UserTableRow({ user, isCurrent, busy, isLastAdmin, onPatch, onDelete }: UserRowProps) {
  return (
    <TableRow hover>
      <TableCell>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            {user.username}
          </Typography>
          {isCurrent && <Chip size="small" label="我" />}
        </Stack>
      </TableCell>

      <TableCell>
        <TextField
          select
          size="small"
          variant="standard"
          value={user.role}
          disabled={busy || (isLastAdmin && user.role === 'admin')}
          onChange={(event) => onPatch({ role: event.target.value as Role })}
          sx={{ minWidth: 104 }}
        >
          <MenuItem value="user">普通用户</MenuItem>
          <MenuItem value="admin">管理员</MenuItem>
        </TextField>
      </TableCell>

      <TableCell>
        <Chip
          size="small"
          variant="outlined"
          color={user.enabled ? 'success' : 'error'}
          label={user.enabled ? '启用' : '已禁用'}
        />
      </TableCell>

      <TableCell sx={{ whiteSpace: 'nowrap', color: 'text.secondary' }}>
        {formatTime(user.created_at)}
      </TableCell>

      <TableCell sx={{ whiteSpace: 'nowrap', color: 'text.secondary' }}>
        {formatTime(user.last_login_at)}
      </TableCell>

      <TableCell align="right">
        <Stack
          direction="row"
          spacing={1}
          sx={{ alignItems: 'center', justifyContent: 'flex-end' }}
        >
          <Tooltip title={user.enabled ? '停用（会撤销全部会话）' : '启用'}>
            <Switch
              size="small"
              checked={user.enabled}
              disabled={busy || (isLastAdmin && user.enabled)}
              onChange={(event) => onPatch({ enabled: event.target.checked })}
            />
          </Tooltip>

          <Tooltip title={isLastAdmin ? '系统里最后一个启用中的管理员，不能删除' : ''}>
            <span>
              <Button
                size="small"
                color="error"
                disabled={busy || isLastAdmin}
                onClick={onDelete}
                sx={{ minWidth: 0 }}
              >
                删除
              </Button>
            </span>
          </Tooltip>
        </Stack>
      </TableCell>
    </TableRow>
  );
}
