import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Divider from '@mui/material/Divider';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';

import { useAuth } from 'src/auth';
import { CONFIG } from 'src/global-config';

import { HealthView } from 'src/sections/health';

// ----------------------------------------------------------------------

const metadata = { title: `${CONFIG.appName} · 后台` };

export default function HomePage() {
  const { user } = useAuth();

  return (
    <>
      <title>{metadata.title}</title>

      <Stack spacing={3}>
        <div>
          <Typography variant="h4" sx={{ mb: 1 }}>
            你好，{user?.username ?? '访客'}
          </Typography>
          <Typography variant="body1" sx={{ color: 'text.secondary' }}>
            会话在服务端，刷新页面不会掉线。版本管理、检索、问答在后续 ticket 落地。
          </Typography>
        </div>

        <Card variant="outlined">
          <CardContent>
            <Typography variant="h6" sx={{ mb: 2 }}>
              当前账号
            </Typography>

            <Divider sx={{ mb: 2 }} />

            <Stack spacing={1.5}>
              <Row label="用户名" value={user?.username ?? '—'} />
              <Row
                label="角色"
                value={user?.role === 'admin' ? '管理员 (admin)' : '普通用户 (user)'}
              />
              <Row label="最近登录" value={user?.last_login_at ?? '—'} />
            </Stack>
          </CardContent>
        </Card>

        <HealthView />
      </Stack>
    </>
  );
}

// ----------------------------------------------------------------------

type RowProps = {
  label: string;
  value: string;
};

function Row({ label, value }: RowProps) {
  return (
    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={{ xs: 0.5, sm: 2 }}>
      <Typography variant="body2" sx={{ color: 'text.secondary', minWidth: 80 }}>
        {label}
      </Typography>
      <Typography variant="body2">{value}</Typography>
    </Stack>
  );
}
