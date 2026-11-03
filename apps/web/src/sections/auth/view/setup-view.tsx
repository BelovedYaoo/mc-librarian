import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';
import { useState } from 'react';
import { useNavigate } from 'react-router';

import { useAuth } from 'src/auth';

import { paths } from 'src/routes/paths';

// ----------------------------------------------------------------------

/** 最短 8 位，与契约 `SetupRequest.password.minLength` 一致。 */
const MIN_PASSWORD_LENGTH = 8;

export function SetupView() {
  const { completeSetup } = useAuth();
  const navigate = useNavigate();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [confirmPassword, setConfirmPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();

    if (password !== confirmPassword) {
      setError('两次输入的密码不一致');
      return;
    }

    setSubmitting(true);
    setError(null);

    try {
      // 创建成功即下发会话 cookie：直接进后台，不需要再登录一次。
      await completeSetup(username.trim(), password);
      navigate(paths.home, { replace: true });
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Stack component="form" spacing={2.5} onSubmit={handleSubmit} noValidate>
      <Alert severity="info">
        这是首次启动：创建一个管理员账号后，setup 会永久关闭，之后的账号由管理员创建。
      </Alert>

      {error && <Alert severity="error">{error}</Alert>}

      <TextField
        label="管理员用户名"
        value={username}
        onChange={(event) => setUsername(event.target.value)}
        autoComplete="username"
        helperText="3–32 位，允许字母、数字、下划线、点和短横线"
        autoFocus
        fullWidth
        required
      />

      <TextField
        label="密码"
        type="password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        autoComplete="new-password"
        helperText={`至少 ${MIN_PASSWORD_LENGTH} 位`}
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
        {submitting ? '创建中…' : '创建管理员并进入后台'}
      </Button>

      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        密码用 argon2id 哈希后存入控制面数据库，服务端不保存明文。
      </Typography>
    </Stack>
  );
}
