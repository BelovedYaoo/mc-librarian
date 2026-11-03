import type { TokenCreated, TokenInfo } from 'src/lib/api-client';

import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogTitle from '@mui/material/DialogTitle';
import Divider from '@mui/material/Divider';
import Stack from '@mui/material/Stack';
import Table from '@mui/material/Table';
import TableBody from '@mui/material/TableBody';
import TableCell from '@mui/material/TableCell';
import TableContainer from '@mui/material/TableContainer';
import TableHead from '@mui/material/TableHead';
import TableRow from '@mui/material/TableRow';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';
import { useCallback, useEffect, useState } from 'react';

import { createToken, listTokens, revokeToken } from 'src/lib/api-client';

// ----------------------------------------------------------------------

/**
 * 「访问令牌」区块：列出当前账号的 PAT（只显示前缀）、创建（明文只展示一次）、吊销。
 * 权限与当前会话完全等价——脚本拿 Bearer 头即可调用同一批接口。
 */
export function TokensView() {
  const [tokens, setTokens] = useState<TokenInfo[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [created, setCreated] = useState<TokenCreated | null>(null);
  const [name, setName] = useState('');
  const [days, setDays] = useState('365');
  const [submitting, setSubmitting] = useState(false);
  const [revokingId, setRevokingId] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    setError(null);

    try {
      const data = await listTokens();
      setTokens(data.tokens ?? []);
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const openCreate = () => {
    setCreated(null);
    setName('');
    setDays('365');
    setCopied(false);
    setCreateOpen(true);
  };

  const closeCreate = () => {
    setCreateOpen(false);
    setCreated(null);
  };

  const handleCreate = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);

    try {
      const result = await createToken({ name: name.trim(), expires_in_days: Number(days) });
      setCreated(result);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSubmitting(false);
    }
  };

  const handleRevoke = async (tokenId: string) => {
    if (!window.confirm('吊销后使用该令牌的脚本会立即失效，确定吊销吗？')) return;

    setRevokingId(tokenId);
    setError(null);

    try {
      await revokeToken(tokenId);
      await load();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setRevokingId(null);
    }
  };

  const handleCopy = async (value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };

  return (
    <Card variant="outlined">
      <CardContent>
        <Stack direction="row" spacing={2} sx={{ alignItems: 'center', mb: 2 }}>
          <Typography variant="h6">访问令牌</Typography>
          <Box sx={{ flex: '1 1 auto' }} />
          <Button size="small" variant="contained" onClick={openCreate}>
            新建令牌
          </Button>
        </Stack>

        <Divider sx={{ mb: 2 }} />

        <Typography variant="body2" sx={{ color: 'text.secondary', mb: 2 }}>
          供脚本用 <code>Authorization: Bearer mcl_…</code> 调用，权限与当前账号完全一致。明文只在创建时显示一次。
        </Typography>

        {error && (
          <Alert severity="error" sx={{ mb: 2 }}>
            {error}
          </Alert>
        )}

        {renderList()}
      </CardContent>

      <Dialog open={createOpen} onClose={closeCreate} fullWidth maxWidth="sm">
        <DialogTitle>{created ? '令牌已创建' : '新建访问令牌'}</DialogTitle>

        {created ? (
          <>
            <DialogContent>
              <Alert severity="warning" sx={{ mb: 2 }}>
                明文令牌只显示这一次，关闭后无法再次查看，请立即复制并妥善保存。
              </Alert>

              <Box
                sx={{
                  p: 1.5,
                  border: '1px solid',
                  borderColor: 'divider',
                  borderRadius: 1,
                  bgcolor: 'action.hover',
                }}
              >
                <Typography
                  variant="body2"
                  sx={{ fontFamily: 'monospace', wordBreak: 'break-all' }}
                >
                  {created.token}
                </Typography>
              </Box>
            </DialogContent>

            <DialogActions>
              <Button onClick={() => void handleCopy(created.token)}>
                {copied ? '已复制' : '复制'}
              </Button>
              <Button variant="contained" onClick={closeCreate}>
                我已保存
              </Button>
            </DialogActions>
          </>
        ) : (
          <Box component="form" onSubmit={handleCreate}>
            <DialogContent>
              <Stack spacing={2}>
                <TextField
                  label="名称"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="例如：CI 脚本"
                  autoFocus
                  fullWidth
                  required
                />
                <TextField
                  label="有效期（天）"
                  type="number"
                  value={days}
                  onChange={(event) => setDays(event.target.value)}
                  fullWidth
                  slotProps={{ htmlInput: { min: 1, max: 3650 } }}
                />
              </Stack>
            </DialogContent>

            <DialogActions>
              <Button onClick={closeCreate}>取消</Button>
              <Button type="submit" variant="contained" disabled={submitting}>
                {submitting ? '创建中…' : '创建'}
              </Button>
            </DialogActions>
          </Box>
        )}
      </Dialog>
    </Card>
  );

  // ----------------------------------------------------------------------

  function renderList() {
    if (tokens === null) {
      return (
        <Stack sx={{ py: 3, alignItems: 'center' }}>
          <CircularProgress size={20} />
        </Stack>
      );
    }

    if (tokens.length === 0) {
      return (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          还没有访问令牌。
        </Typography>
      );
    }

    return (
      <TableContainer>
        <Table size="small">
          <TableHead>
            <TableRow>
              <TableCell>名称</TableCell>
              <TableCell>前缀</TableCell>
              <TableCell>最近使用</TableCell>
              <TableCell>过期时间</TableCell>
              <TableCell align="right">操作</TableCell>
            </TableRow>
          </TableHead>

          <TableBody>
            {tokens.map((token) => (
              <TableRow key={token.id} hover>
                <TableCell>{token.name}</TableCell>
                <TableCell sx={{ fontFamily: 'monospace' }}>{token.prefix}…</TableCell>
                <TableCell>{formatTime(token.last_used_at)}</TableCell>
                <TableCell>{renderExpiry(token)}</TableCell>
                <TableCell align="right">
                  <Button
                    size="small"
                    color="error"
                    disabled={revokingId === token.id}
                    onClick={() => void handleRevoke(token.id)}
                  >
                    {revokingId === token.id ? '吊销中…' : '吊销'}
                  </Button>
                </TableCell>
              </TableRow>
            ))}
          </TableBody>
        </Table>
      </TableContainer>
    );
  }

  function renderExpiry(token: TokenInfo) {
    if (!token.expires_at) return '—';

    const expired = new Date(token.expires_at).getTime() <= Date.now();

    return (
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
        <span>{formatTime(token.expires_at)}</span>
        {expired && <Chip size="small" color="error" label="已过期" />}
      </Stack>
    );
  }
}

// ----------------------------------------------------------------------

/** 后端时间是 UTC 的 `YYYY-MM-DDTHH:MM:SSZ`，按本地时区展示。 */
function formatTime(value?: string): string {
  if (!value) return '—';

  const date = new Date(value);

  return Number.isNaN(date.getTime()) ? value : date.toLocaleString('zh-CN');
}
