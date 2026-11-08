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
import { useState } from 'react';

import { CONFIG } from 'src/global-config';

import { useHealth } from 'src/hooks/use-health';

// ----------------------------------------------------------------------

/**
 * 后端健康状态。
 *
 * 之前把 `/v1/health` 的原始 JSON 直接摆出来（`{"status":"ok","version":"0.2.0"}`）——
 * 那是给排障看的，不是给人读的。现在拆成四块人话信息：状态 / 版本 / 响应耗时 / 检查时间，
 * 原始响应收进「查看原始响应」里，需要时再展开。
 */
export function HealthView() {
  const { state, refresh } = useHealth();
  const [showRaw, setShowRaw] = useState(false);

  const ok = state.status === 'ok';

  return (
    <Card>
      <CardContent>
        <Stack direction="row" spacing={2} sx={{ alignItems: 'center', mb: 2 }}>
          <Typography variant="h6">后端健康状态</Typography>
          <Box sx={{ flex: '1 1 auto' }} />
          {state.status === 'loading' ? (
            <CircularProgress size={16} />
          ) : (
            <Chip
              size="small"
              color={ok ? 'success' : 'error'}
              label={ok ? '运行正常' : '无法连接'}
            />
          )}
          <Button
            size="small"
            variant="outlined"
            onClick={refresh}
            disabled={state.status === 'loading'}
          >
            重新检测
          </Button>
        </Stack>

        <Divider sx={{ mb: 2 }} />

        <Box
          sx={{
            display: 'grid',
            gap: 2,
            gridTemplateColumns: { xs: '1fr 1fr', sm: 'repeat(4, 1fr)' },
          }}
        >
          <Stat
            label="状态"
            value={ok ? '正常' : state.status === 'loading' ? '检测中' : '不可达'}
          />
          <Stat label="后端版本" value={ok ? (state.data.version ?? '—') : '—'} />
          <Stat label="响应耗时" value={ok ? `${state.latencyMs} ms` : '—'} />
          <Stat
            label="检查时间"
            value={state.status === 'loading' ? '—' : state.checkedAt.toLocaleTimeString('zh-CN')}
          />
        </Box>

        <Divider sx={{ my: 2 }} />

        <Stack spacing={1}>
          <Row label="接口地址" value={`${CONFIG.serverUrl}/v1/health`} />

          {ok && (
            <Button
              size="small"
              onClick={() => setShowRaw((current) => !current)}
              sx={{ alignSelf: 'flex-start', minWidth: 0 }}
            >
              {showRaw ? '收起原始响应' : '查看原始响应'}
            </Button>
          )}

          {ok && showRaw && (
            <Box
              component="pre"
              sx={{
                m: 0,
                p: 1.5,
                borderRadius: 1,
                bgcolor: 'background.neutral',
                fontFamily: 'monospace',
                fontSize: 12,
                overflowX: 'auto',
              }}
            >
              {JSON.stringify(state.data, null, 2)}
            </Box>
          )}
        </Stack>

        {state.status === 'error' && (
          <Alert severity="error" sx={{ mt: 2 }}>
            无法读取后端健康状态：{state.message}
            <br />
            确认后端已启动（默认 <code>http://127.0.0.1:8787</code>），或检查 <code>/api</code>{' '}
            代理。
          </Alert>
        )}
      </CardContent>
    </Card>
  );
}

// ----------------------------------------------------------------------

function Stat({ label, value }: { label: string; value: string }) {
  return (
    <Box>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        {label}
      </Typography>
      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
        {value}
      </Typography>
    </Box>
  );
}

function Row({ label, value }: { label: string; value: string }) {
  return (
    <Stack direction="row" spacing={2}>
      <Typography variant="body2" sx={{ color: 'text.secondary', minWidth: 72 }}>
        {label}
      </Typography>
      <Typography variant="body2" sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>
        {value}
      </Typography>
    </Stack>
  );
}
