import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Divider from '@mui/material/Divider';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';

import { CONFIG } from 'src/global-config';

import { useHealth } from 'src/hooks/use-health';

// ----------------------------------------------------------------------

/** 后端健康状态卡片：前端空壳页面唯一的数据来源。 */
export function HealthView() {
  const { state, refresh } = useHealth();

  return (
    <Card>
      <CardContent>
        <Stack direction="row" spacing={2} sx={{ alignItems: 'center', mb: 2 }}>
          <Typography variant="h6">后端健康状态</Typography>
          <Box sx={{ flex: '1 1 auto' }} />
          {renderChip()}
          <Button size="small" variant="outlined" onClick={refresh} disabled={isLoading()}>
            重新检测
          </Button>
        </Stack>

        <Divider sx={{ mb: 2 }} />

        <Stack spacing={1.5}>
          <Row label="端点" value={`${CONFIG.serverUrl}/v1/health`} />
          <Row label="返回" value={renderPayload()} mono />
          <Row label="检查时间" value={renderCheckedAt()} />
        </Stack>

        {state.status === 'error' && (
          <Alert severity="error" sx={{ mt: 3 }}>
            无法读取后端健康状态：{state.message}
            <br />
            确认后端已启动（默认 <code>http://127.0.0.1:8787</code>），或检查 <code>/api</code>{' '}
            代理。
          </Alert>
        )}
      </CardContent>
    </Card>
  );

  // ----------------------------------------------------------------------

  function isLoading() {
    return state.status === 'loading';
  }

  function renderChip() {
    if (state.status === 'loading') {
      return <CircularProgress size={16} />;
    }

    return state.status === 'ok' ? (
      <Chip size="small" color="success" label="ok" />
    ) : (
      <Chip size="small" color="error" label="不可达" />
    );
  }

  function renderPayload() {
    if (state.status === 'ok') {
      return JSON.stringify(state.data);
    }

    return state.status === 'loading' ? '请求中…' : '—';
  }

  function renderCheckedAt() {
    if (state.status === 'loading') return '—';

    return state.checkedAt.toLocaleString('zh-CN');
  }
}

// ----------------------------------------------------------------------

type RowProps = {
  label: string;
  value: string;
  mono?: boolean;
};

function Row({ label, value, mono = false }: RowProps) {
  return (
    <Stack direction={{ xs: 'column', sm: 'row' }} spacing={{ xs: 0.5, sm: 2 }}>
      <Typography variant="body2" sx={{ color: 'text.secondary', minWidth: 80 }}>
        {label}
      </Typography>
      <Typography variant="body2" sx={{ fontFamily: mono ? 'monospace' : undefined }}>
        {value}
      </Typography>
    </Stack>
  );
}
