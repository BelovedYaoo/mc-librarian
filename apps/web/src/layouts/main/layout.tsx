import Container from '@mui/material/Container';
import Typography from '@mui/material/Typography';
import Box from '@mui/material/Box';

import { CONFIG } from 'src/global-config';

import { HeaderSection, LayoutSection, MainSection } from 'src/layouts/core';

// ----------------------------------------------------------------------

export type MainLayoutProps = {
  children: React.ReactNode;
};

export function MainLayout({ children }: MainLayoutProps) {
  return (
    <LayoutSection
      headerSection={
        <HeaderSection>
          <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
            {CONFIG.appName}
          </Typography>
          <Box sx={{ flex: '1 1 auto' }} />
          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
            v{CONFIG.appVersion}
          </Typography>
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
