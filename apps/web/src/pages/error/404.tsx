import Button from '@mui/material/Button';
import Typography from '@mui/material/Typography';
import { Link as RouterLink } from 'react-router';

import { CONFIG } from 'src/global-config';

import { paths } from 'src/routes/paths';

import { MainLayout } from 'src/layouts/main';

// ----------------------------------------------------------------------

const metadata = { title: `404 页面不存在 - ${CONFIG.appName}` };

export default function Page404() {
  return (
    <MainLayout>
      <title>{metadata.title}</title>

      <Typography variant="h4" sx={{ mb: 2 }}>
        404 页面不存在
      </Typography>

      <Button component={RouterLink} to={paths.home} variant="contained">
        回到首页
      </Button>
    </MainLayout>
  );
}
