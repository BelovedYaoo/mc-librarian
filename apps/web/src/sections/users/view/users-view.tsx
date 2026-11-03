import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Divider from '@mui/material/Divider';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import Switch from '@mui/material/Switch';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';
import { useCallback, useEffect, useState } from 'react';

import type { CreatedUser, PatchUserRequest, User } from 'src/lib/api-client';

import { useAuth } from 'src/auth';
import { createUser, listUsers, patchUser } from 'src/lib/api-client';

// ----------------------------------------------------------------------

type Role = User['role'];

type UsersState =
  | { status: 'loading' }
  | { status: 'ready'; users: User[] }
  | { status: 'error'; message: string };

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

      <Card variant="outlined">
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

      <Card variant="outlined">
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

          {state.status === 'ready' &&
            state.users.map((item) => (
              <UserRow
                key={item.id}
                user={item}
                isCurrent={item.id === currentUser?.id}
                busy={busyId === item.id}
                onPatch={(body) => void handlePatch(item, body)}
              />
            ))}
        </CardContent>
      </Card>
    </Stack>
  );
}

// ----------------------------------------------------------------------

type UserRowProps = {
  user: User;
  isCurrent: boolean;
  busy: boolean;
  onPatch: (body: PatchUserRequest) => void;
};

function UserRow({ user, isCurrent, busy, onPatch }: UserRowProps) {
  return (
    <Stack
      direction={{ xs: 'column', sm: 'row' }}
      spacing={2}
      sx={{
        alignItems: { sm: 'center' },
        py: 1.5,
        borderBottom: 1,
        borderColor: 'divider',
        '&:last-of-type': { borderBottom: 'none' },
      }}
    >
      <Box sx={{ flex: '1 1 auto', minWidth: 0 }}>
        <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
          <Typography variant="body2" sx={{ fontWeight: 600 }}>
            {user.username}
          </Typography>
          {isCurrent && <Chip size="small" label="我" />}
          {!user.enabled && <Chip size="small" color="error" label="已禁用" />}
        </Stack>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          最近登录：{user.last_login_at ?? '从未'}
        </Typography>
      </Box>

      <TextField
        select
        size="small"
        label="角色"
        value={user.role}
        disabled={busy}
        onChange={(event) => onPatch({ role: event.target.value as Role })}
        sx={{ minWidth: 130 }}
      >
        <MenuItem value="user">普通用户</MenuItem>
        <MenuItem value="admin">管理员</MenuItem>
      </TextField>

      <Stack direction="row" spacing={1} sx={{ alignItems: 'center', minWidth: 96 }}>
        <Switch
          size="small"
          checked={user.enabled}
          disabled={busy}
          onChange={(event) => onPatch({ enabled: event.target.checked })}
        />
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {user.enabled ? '启用' : '禁用'}
        </Typography>
      </Stack>
    </Stack>
  );
}
