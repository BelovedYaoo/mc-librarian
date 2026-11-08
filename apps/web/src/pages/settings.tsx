import Box from '@mui/material/Box';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';

import { useAuth } from 'src/auth';
import { CONFIG } from 'src/global-config';

import { SettingsView } from 'src/sections/settings';
import { TokensView } from 'src/sections/tokens';

// ----------------------------------------------------------------------

const metadata = { title: `${CONFIG.appName} · 设置` };

export default function SettingsPage() {
  const { user } = useAuth();
  // 实例设置只有 admin 能读写（后端每个端点仍会独立校验 403）。
  const isAdmin = user?.role === 'admin';

  return (
    <>
      <title>{metadata.title}</title>

      {/* 两列布局：设置卡片按 2 列排（lg 起），单列时输入框仍不会被拉成长条。 */}
      <Box sx={{ maxWidth: 1160, mx: 'auto' }}>
        <Stack spacing={3}>
          <div>
            <Typography variant="h4" sx={{ mb: 1 }}>
              设置
            </Typography>
            <Typography variant="body1" sx={{ color: 'text.secondary' }}>
              {isAdmin
                ? '实例设置（模型 / 问答 / 索引 / 会话 / 告警）与当前账号的个人访问令牌。保存后立即生效。'
                : `管理当前账号（${user?.username ?? '—'}）的个人访问令牌。`}
            </Typography>
          </div>

          {isAdmin && <SettingsView />}

          <TokensView />
        </Stack>
      </Box>
    </>
  );
}
