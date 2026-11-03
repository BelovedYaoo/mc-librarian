import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Container from '@mui/material/Container';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';

import { CONFIG } from 'src/global-config';

import { LayoutSection, MainSection } from 'src/layouts/core';

// ----------------------------------------------------------------------

export type AuthLayoutProps = {
  /** 卡片主标题，例如「登录」/「初始化管理员」。 */
  title: string;
  subtitle: string;
  children: React.ReactNode;
};

/** 未登录态的两个页面（`/setup`、`/login`）共用的居中卡片外壳。 */
export function AuthLayout({ title, subtitle, children }: AuthLayoutProps) {
  return (
    <LayoutSection>
      <MainSection>
        <Container maxWidth="sm" sx={{ py: { xs: 6, md: 12 } }}>
          <Stack spacing={3}>
            <div>
              <Typography variant="h5" sx={{ fontWeight: 700 }}>
                {CONFIG.appName}
              </Typography>
              <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                Minecraft 源码知识库与中文问答服务
              </Typography>
            </div>

            <Card variant="outlined">
              <CardContent sx={{ p: { xs: 3, md: 4 } }}>
                <Stack spacing={3}>
                  <div>
                    <Typography variant="h6">{title}</Typography>
                    <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                      {subtitle}
                    </Typography>
                  </div>

                  {children}
                </Stack>
              </CardContent>
            </Card>

            <Typography variant="caption" sx={{ color: 'text.secondary' }}>
              v{CONFIG.appVersion}
            </Typography>
          </Stack>
        </Container>
      </MainSection>
    </LayoutSection>
  );
}
