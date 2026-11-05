import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import Container from '@mui/material/Container';
import Typography from '@mui/material/Typography';
import { useState } from 'react';
import { useNavigate } from 'react-router';

import { useAuth } from 'src/auth';
import { ThemeModeToggle } from 'src/components/theme-mode-toggle';
import { CONFIG } from 'src/global-config';

import { paths } from 'src/routes/paths';

import { HeaderSection, LayoutSection, MainSection, NavSection } from 'src/layouts/core';

// ----------------------------------------------------------------------

export type MainLayoutProps = {
  children: React.ReactNode;
  /**
   * 内容列宽：`'xl'`（1536px，默认）留给表单/列表/表格页；`false` = 全宽，由页面自己排布。
   * 问答页用 `false`：它要的是聊天工作台（左栏 + 消息区 + 钉底输入区），不是文档页的窄栏。
   */
  maxWidth?: 'md' | 'xl' | false;
};

/** 受保护的后台外壳：左侧导航 + 顶部当前用户与登出入口。 */
export function MainLayout({ children, maxWidth = 'xl' }: MainLayoutProps) {
  const { user, signOut } = useAuth();
  const navigate = useNavigate();
  const [signingOut, setSigningOut] = useState(false);
  const fullWidth = maxWidth === false;

  const handleSignOut = async () => {
    setSigningOut(true);

    try {
      await signOut();
      navigate(paths.login, { replace: true });
    } finally {
      setSigningOut(false);
    }
  };

  // admin-only 的条目只对管理员显示——隐藏菜单只是体验，后端每个端点会独立校验。
  const navItems = [
    { label: '概览', to: paths.home, end: true },
    { label: '版本', to: paths.versions },
    { label: '检索', to: paths.browse },
    { label: '问答', to: paths.ask },
    ...(user?.role === 'admin'
      ? [
          { label: '任务', to: paths.jobs },
          { label: '用户', to: paths.users },
          { label: '用量', to: paths.usage },
          { label: '审计', to: paths.audit },
        ]
      : []),
  ];

  return (
    <LayoutSection
      // 全宽页（问答）自己管滚动：外壳钉在视口高度上，滚动条只出现在页面内部。
      sx={fullWidth ? { height: '100dvh', overflow: 'hidden' } : undefined}
      headerSection={
        <HeaderSection>
          <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
            {CONFIG.appName}
          </Typography>

          <Box sx={{ flex: '1 1 auto' }} />

          <ThemeModeToggle sx={{ mr: 1 }} />

          <Button size="small" color="inherit" onClick={() => navigate(paths.home)} sx={{ mr: 1 }}>
            首页
          </Button>

          <Button
            size="small"
            color="inherit"
            onClick={() => navigate(paths.settings)}
            sx={{ mr: 2 }}
          >
            访问令牌
          </Button>

          {user && (
            <>
              <Typography variant="body2" sx={{ color: 'text.secondary', mr: 1 }}>
                {user.username}
              </Typography>

              <Chip
                size="small"
                label={user.role === 'admin' ? '管理员' : '普通用户'}
                color={user.role === 'admin' ? 'primary' : 'default'}
                sx={{ mr: 2 }}
              />
            </>
          )}

          <Button size="small" variant="outlined" onClick={handleSignOut} disabled={signingOut}>
            {signingOut ? '登出中…' : '登出'}
          </Button>
        </HeaderSection>
      }
    >
      <MainSection sx={fullWidth ? { minHeight: 0, overflow: 'hidden' } : undefined}>
        <Box
          sx={{
            display: 'flex',
            flex: '1 1 auto',
            flexDirection: { xs: 'column', md: 'row' },
            // 固定高度的外壳里，flex 子项必须能收缩，否则会溢出被裁掉。
            minHeight: 0,
          }}
        >
          <NavSection items={navItems} />

          <Container
            maxWidth={maxWidth}
            sx={{
              flex: '1 1 auto',
              minWidth: 0,
              ...(fullWidth
                ? { p: 0, display: 'flex', flexDirection: 'column', minHeight: 0, overflow: 'hidden' }
                : { py: { xs: 5, md: 8 } }),
            }}
          >
            {children}
          </Container>
        </Box>
      </MainSection>
    </LayoutSection>
  );
}
