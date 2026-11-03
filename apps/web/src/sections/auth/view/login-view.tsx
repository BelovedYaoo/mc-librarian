import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import { useState } from 'react';
import { useNavigate } from 'react-router';

import { useAuth } from 'src/auth';

import { paths } from 'src/routes/paths';

// ----------------------------------------------------------------------

export function LoginView() {
  const { signIn } = useAuth();
  const navigate = useNavigate();

  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);

  const handleSubmit = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);

    try {
      await signIn(username.trim(), password);
      navigate(paths.home, { replace: true });
    } catch (caught) {
      // 后端对「用户不存在」与「密码错」返回同一句文案，这里直接展示。
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Stack component="form" spacing={2.5} onSubmit={handleSubmit} noValidate>
      {error && <Alert severity="error">{error}</Alert>}

      <TextField
        label="用户名"
        value={username}
        onChange={(event) => setUsername(event.target.value)}
        autoComplete="username"
        autoFocus
        fullWidth
        required
      />

      <TextField
        label="密码"
        type="password"
        value={password}
        onChange={(event) => setPassword(event.target.value)}
        autoComplete="current-password"
        fullWidth
        required
      />

      <Button type="submit" variant="contained" size="large" disabled={submitting}>
        {submitting ? '登录中…' : '登录'}
      </Button>
    </Stack>
  );
}
