import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';

import { useAuth } from 'src/auth';
import { CONFIG } from 'src/global-config';

import { TokensView } from 'src/sections/tokens';

// ----------------------------------------------------------------------

const metadata = { title: `${CONFIG.appName} · 设置` };

export default function SettingsPage() {
  const { user } = useAuth();

  return (
    <>
      <title>{metadata.title}</title>

      <Stack spacing={3}>
        <div>
          <Typography variant="h4" sx={{ mb: 1 }}>
            设置
          </Typography>
          <Typography variant="body1" sx={{ color: 'text.secondary' }}>
            管理当前账号（{user?.username ?? '—'}）的个人访问令牌。
          </Typography>
        </div>

        <TokensView />
      </Stack>
    </>
  );
}
