import Typography from '@mui/material/Typography';
import Stack from '@mui/material/Stack';

import { CONFIG } from 'src/global-config';

import { HealthView } from 'src/sections/health';

// ----------------------------------------------------------------------

const metadata = { title: `${CONFIG.appName} · 骨架` };

export default function HomePage() {
  return (
    <>
      <title>{metadata.title}</title>

      <Stack spacing={3}>
        <div>
          <Typography variant="h4" sx={{ mb: 1 }}>
            {CONFIG.appName}
          </Typography>
          <Typography variant="body1" sx={{ color: 'text.secondary' }}>
            Minecraft 源码知识库与中文问答服务。当前只有骨架：后端提供 <code>/v1/health</code> 与{' '}
            <code>/openapi.json</code>，版本管理、检索、问答在后续 ticket 落地。
          </Typography>
        </div>

        <HealthView />
      </Stack>
    </>
  );
}
