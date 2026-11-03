import type { VersionInfo } from 'src/lib/api-client';

import { useState } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import Divider from '@mui/material/Divider';
import Drawer from '@mui/material/Drawer';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';
import { useNavigate } from 'react-router';

import { useAuth } from 'src/auth';

import { useAdminSettings, useVersions } from 'src/hooks';

import { ApiError, createJob } from 'src/lib/api-client';

import { paths } from 'src/routes/paths';

// ----------------------------------------------------------------------

/** 版本列表 + 新建索引抽屉（仅 admin 可见可操作）。 */
export function VersionsView() {
  const { user } = useAuth();
  const { state, refresh } = useVersions();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const isAdmin = user?.role === 'admin';
  const { state: settingsState } = useAdminSettings(isAdmin);
  // 只在明确读到 `index.enabled === false` 时才隐藏入口；读不到（加载中/失败）不假设关闭。
  const indexDisabled =
    settingsState.status === 'ready' && settingsState.settings?.index?.enabled === false;

  return (
    <Stack spacing={3}>
      <Stack direction="row" spacing={2} sx={{ alignItems: 'center' }}>
        <Typography variant="h4">版本</Typography>
        <Box sx={{ flex: '1 1 auto' }} />

        <Button size="small" variant="outlined" onClick={refresh}>
          刷新
        </Button>

        {isAdmin && !indexDisabled && (
          <Button size="small" variant="contained" onClick={() => setDrawerOpen(true)}>
            新建索引
          </Button>
        )}
      </Stack>

      {isAdmin && indexDisabled && (
        <Alert severity="info">
          本实例已关闭索引构建，请在本地工作站生成索引后用「注册索引」上传
        </Alert>
      )}

      <Card variant="outlined">
        <CardContent>
          <Typography variant="h6" sx={{ mb: 2 }}>
            已索引版本
          </Typography>

          <Divider sx={{ mb: 2 }} />

          {renderList()}
        </CardContent>
      </Card>

      {isAdmin && !indexDisabled && (
        <IndexDrawer
          open={drawerOpen}
          onClose={() => setDrawerOpen(false)}
          onCreated={refresh}
        />
      )}
    </Stack>
  );

  // ----------------------------------------------------------------------

  function renderList() {
    if (state.status === 'loading') {
      return (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          加载中…
        </Typography>
      );
    }

    if (state.status === 'error') {
      return <Alert severity="error">读取版本列表失败：{state.message}</Alert>;
    }

    if (state.versions.length === 0) {
      return (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          还没有已索引版本。管理员可以用右上角「新建索引」提交一次任务。
        </Typography>
      );
    }

    return (
      <Stack divider={<Divider flexItem />} spacing={2}>
        {state.versions.map((item) => (
          <VersionRow key={item.version} version={item} />
        ))}
      </Stack>
    );
  }
}

// ----------------------------------------------------------------------

function VersionRow({ version }: { version: VersionInfo }) {
  return (
    <Stack spacing={1}>
      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
        <Typography variant="subtitle1" sx={{ fontWeight: 700 }}>
          {version.version}
        </Typography>

        {version.obfuscated && <Chip size="small" label="已 remap" />}
        <Chip
          size="small"
          variant="outlined"
          color={version.has_summaries ? 'success' : 'default'}
          label={version.has_summaries ? '有中文摘要' : '无摘要'}
        />
      </Stack>

      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        文件 {version.files.toLocaleString('zh-CN')} · token 估算{' '}
        {version.tokens.toLocaleString('zh-CN')} · 索引于 {formatTime(version.indexed_at)}
      </Typography>
    </Stack>
  );
}

function formatTime(value: string): string {
  const parsed = new Date(value);

  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN');
}

// ----------------------------------------------------------------------

type IndexDrawerProps = {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
};

function IndexDrawer({ open, onClose, onCreated }: IndexDrawerProps) {
  const navigate = useNavigate();
  const [version, setVersion] = useState('');
  const [sourceDir, setSourceDir] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);

    try {
      // 留空即省略 `options`：后端从官方 jar 跑完整五段；给 `options` 但 `source_dir` 为空会 422。
      await createJob(
        sourceDir
          ? { kind: 'index', version, options: { source_dir: sourceDir, force: false } }
          : { kind: 'index', version }
      );

      onCreated();
      onClose();
      navigate(paths.jobs);
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Drawer anchor="right" open={open} onClose={onClose}>
      <Box component="form" onSubmit={handleSubmit} sx={{ width: 380, p: 3 }}>
        <Typography variant="h6" sx={{ mb: 2 }}>
          新建索引任务
        </Typography>

        <Stack spacing={2}>
          <TextField
            label="版本"
            value={version}
            onChange={(event) => setVersion(event.target.value)}
            placeholder="26.3"
            required
            fullWidth
          />

          <TextField
            label="源码目录（可选）"
            value={sourceDir}
            onChange={(event) => setSourceDir(event.target.value)}
            placeholder="/data/mc-librarian/src/26.3"
            helperText="留空 = 从官方 jar 跑完整五段（下载 → 反编译 → 解析 → 索引）；填写 = 跳过下载反编译，直接从该目录解析"
            fullWidth
          />

          {error && <Alert severity="error">{error}</Alert>}

          <Stack direction="row" spacing={1.5} sx={{ justifyContent: 'flex-end' }}>
            <Button onClick={onClose} disabled={submitting}>
              取消
            </Button>
            <Button type="submit" variant="contained" disabled={submitting}>
              {submitting ? '提交中…' : '提交'}
            </Button>
          </Stack>
        </Stack>
      </Box>
    </Drawer>
  );
}
