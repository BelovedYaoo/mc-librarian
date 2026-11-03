import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import CircularProgress from '@mui/material/CircularProgress';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';
import { useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router';

import { useAuth } from 'src/auth';
import { acceptInvite, getInvite } from 'src/lib/api-client';

import { paths } from 'src/routes/paths';

// ----------------------------------------------------------------------

/** 最短 8 位，与契约 `AcceptInviteRequest.password.minLength` 一致。 */
const MIN_PASSWORD_LENGTH = 8;

type InviteState =
  | { status: 'loading' }
  | { status: 'valid'; username: string }
  | { status: 'invalid' }
  | { status: 'error'; message: string };

function messageOf(caught: unknown): string {
  return caught instanceof Error ? caught.message : String(caught);
}

/**
 * `/invite/:token`：被邀请人自设密码。
 *
 * 链接是一次性的（48 小时有效），所以进页面先校验：无效就不给填表。
 * 接受成功后后端直接下发会话 cookie，`refresh()` 拿到当前用户后回后台。
 */
export function InviteView() {
  const { token = '' } = useParams();
  const { refresh } = useAuth();
  const navigate = useNavigate();

  const [state, setState] = useState<InviteState>({ status: 'loading' });
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  useEffect(() => {
    const controller = new AbortController();

    getInvite(token, controller.signal)
      .then((invite) => {
        setState(
          invite.valid
            ? { status: 'valid', username: invite.username ?? '' }
            : { status: 'invalid' }
        );
      })
      .catch((caught: unknown) => {
        if (controller.signal.aborted) return;

        setState({ status: 'error', message: messageOf(caught) });
      });

    return () => controller.abort();
  }, [token]);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    if (password !== confirmPassword) {
      setError('两次输入的密码不一致');
      return;
    }

    setSubmitting(true);
    setError(null);

    try {
      await acceptInvite(token, { password });
      await refresh();
      navigate(paths.home, { replace: true });
    } catch (caught) {
      setError(messageOf(caught));
    } finally {
      setSubmitting(false);
    }
  };

  if (state.status === 'loading') {
    return (
      <Stack sx={{ alignItems: 'center', py: 2 }}>
        <CircularProgress size={24} />
      </Stack>
    );
  }

  if (state.status === 'invalid') {
    return (
      <Alert severity="warning">
        这个邀请链接无效、已过期或已被使用。请联系管理员重新发一条。
      </Alert>
    );
  }

  if (state.status === 'error') {
    return <Alert severity="error">无法校验邀请链接：{state.message}</Alert>;
  }

  return (
    <Stack component="form" spacing={2.5} onSubmit={handleSubmit} noValidate>
      <Alert severity="info">
        为账号 <b>{state.username}</b> 设置密码。密码由你自己保管，管理员不会知道。
      </Alert>

      {error && <Alert severity="error">{error}</Alert>}

      <TextField
        label="密码"
        type="password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        autoComplete="new-password"
        helperText={`至少 ${MIN_PASSWORD_LENGTH} 位`}
        autoFocus
        fullWidth
        required
      />

      <TextField
        label="确认密码"
        type="password"
        value={confirmPassword}
        onChange={(event) => setConfirmPassword(event.target.value)}
        autoComplete="new-password"
        fullWidth
        required
      />

      <Button type="submit" variant="contained" size="large" disabled={submitting}>
        {submitting ? '提交中…' : '设置密码并进入后台'}
      </Button>

      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        密码用 argon2id 哈希后存入控制面数据库；提交成功后链接立即失效。
      </Typography>
    </Stack>
  );
}
