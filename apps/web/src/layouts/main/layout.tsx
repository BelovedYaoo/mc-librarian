import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import Container from '@mui/material/Container';
import Typography from '@mui/material/Typography';
import { useState } from 'react';
import { useNavigate } from 'react-router';

import { useAuth } from 'src/auth';
import { CONFIG } from 'src/global-config';

import { paths } from 'src/routes/paths';

import { HeaderSection, LayoutSection, MainSection } from 'src/layouts/core';

// ----------------------------------------------------------------------

export type MainLayoutProps = {
  children: React.ReactNode;
};

/** 受保护的后台外壳：顶部展示当前用户名与登出入口。 */
export function MainLayout({ children }: MainLayoutProps) {
  const { user, signOut } = useAuth();
  const navigate = useNavigate();
  const [signingOut, setSigningOut] = useState(false);

  const handleSignOut = async () => {
    setSigningOut(true);

    try {
      await signOut();
      navigate(paths.login, { replace: true });
    } finally {
      setSigningOut(false);
    }
  };

  return (
    <LayoutSection
      headerSection={
        <HeaderSection>
          <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
            {CONFIG.appName}
          </Typography>

          <Box sx={{ flex: '1 1 auto' }} />

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
      <MainSection>
        <Container maxWidth="md" sx={{ py: { xs: 5, md: 8 } }}>
          {children}
        </Container>
      </MainSection>
    </LayoutSection>
  );
}
