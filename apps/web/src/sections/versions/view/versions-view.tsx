import type { VersionInfo } from 'src/lib/api-client';

import { useState } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Checkbox from '@mui/material/Checkbox';
import Chip from '@mui/material/Chip';
import Dialog from '@mui/material/Dialog';
import DialogActions from '@mui/material/DialogActions';
import DialogContent from '@mui/material/DialogContent';
import DialogTitle from '@mui/material/DialogTitle';
import Divider from '@mui/material/Divider';
import Drawer from '@mui/material/Drawer';
import FormControlLabel from '@mui/material/FormControlLabel';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';
import { useNavigate } from 'react-router';

import { useAuth } from 'src/auth';

import { useAdminSettings, useVersions } from 'src/hooks';

import { ApiError, createJob, registerVersion } from 'src/lib/api-client';

import { paths } from 'src/routes/paths';

// ----------------------------------------------------------------------

/** 版本列表 + 新建索引抽屉（仅 admin 可见可操作）。 */
export function VersionsView() {
  const { user } = useAuth();
  const { state, refresh } = useVersions();
  const [drawerOpen, setDrawerOpen] = useState(false);
  const [registerOpen, setRegisterOpen] = useState(false);
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

        {isAdmin && (
          <Button size="small" variant="outlined" onClick={() => setRegisterOpen(true)}>
            注册索引
          </Button>
        )}

        {isAdmin && !indexDisabled && (
          <Button size="small" variant="contained" onClick={() => setDrawerOpen(true)}>
            新建索引
          </Button>
        )}
      </Stack>

      {isAdmin && indexDisabled && (
        <Alert severity="info">
          本实例已关闭索引构建。把本地生成的 <code>db/&lt;版本&gt;.sqlite</code>{' '}
          放进数据根后，用「注册索引」登记
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

      {isAdmin && (
        <RegisterDialog
          open={registerOpen}
          onClose={() => setRegisterOpen(false)}
          onRegistered={refresh}
        />
      )}

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
          还没有已索引版本。管理员可以用右上角「注册索引」登记已上传的索引，或用「新建索引」提交一次任务。
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
  const coverage = coverageLabel(version);

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

      {coverage && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          摘要覆盖 {coverage}
        </Typography>
      )}
    </Stack>
  );
}

/** 摘要覆盖率（已生成 / 类总数，issue #7 规格 §8）；索引未登记或读不到时返回 null。 */
function coverageLabel(version: VersionInfo): string | null {
  if (version.class_count == null) return null;

  return `${(version.summary_count ?? 0).toLocaleString('zh-CN')} / ${version.class_count.toLocaleString('zh-CN')}`;
}

function formatTime(value: string): string {
  const parsed = new Date(value);

  return Number.isNaN(parsed.getTime()) ? value : parsed.toLocaleString('zh-CN');
}

// ----------------------------------------------------------------------

type RegisterDialogProps = {
  open: boolean;
  onClose: () => void;
  onRegistered: () => void;
};

/** 注册已上传的 `db/<version>.sqlite`：部署流程（D9）里索引在本地构建、上传后登记。 */
function RegisterDialog({ open, onClose, onRegistered }: RegisterDialogProps) {
  const [version, setVersion] = useState('');
  // 缺省按「已 remap」：未声明时后端也按此记录，26.x 这类不混淆的版本请手动取消勾选。
  const [obfuscated, setObfuscated] = useState(true);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);

    try {
      await registerVersion({ version, obfuscated });
      onRegistered();
      onClose();
    } catch (cause) {
      setError(cause instanceof ApiError ? cause.message : String(cause));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Dialog open={open} onClose={onClose} fullWidth maxWidth="sm">
      <Box component="form" onSubmit={handleSubmit}>
        <DialogTitle>注册索引</DialogTitle>

        <DialogContent>
          <Stack spacing={2} sx={{ mt: 1 }}>
            <TextField
              label="版本"
              value={version}
              onChange={(event) => setVersion(event.target.value)}
              placeholder="26.3"
              helperText="先把本地生成的 db/<版本>.sqlite 放进数据根，再在这里填版本号"
              autoFocus
              required
              fullWidth
            />

            <FormControlLabel
              control={
                <Checkbox
                  checked={obfuscated}
                  onChange={(event) => setObfuscated(event.target.checked)}
                />
              }
              label="源码经官方映射重映射（remap）"
            />

            {error && <Alert severity="error">{error}</Alert>}
          </Stack>
        </DialogContent>

        <DialogActions>
          <Button onClick={onClose} disabled={submitting}>
            取消
          </Button>
          <Button type="submit" variant="contained" disabled={submitting || version === ''}>
            {submitting ? '注册中…' : '注册'}
          </Button>
        </DialogActions>
      </Box>
    </Dialog>
  );
}

// ----------------------------------------------------------------------

type IndexDrawerProps = {
  open: boolean;
  onClose: () => void;
  onCreated: () => void;
};

/** `options.obfuscated` 的三态：不声明（省略字段）/ 已 remap / 未 remap。 */
type ObfuscatedChoice = 'unset' | 'remapped' | 'not-remapped';

function IndexDrawer({ open, onClose, onCreated }: IndexDrawerProps) {
  const navigate = useNavigate();
  const [version, setVersion] = useState('');
  const [sourceDir, setSourceDir] = useState('');
  const [obfuscated, setObfuscated] = useState<ObfuscatedChoice>('unset');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // `options.obfuscated` 只在给了 `source_dir`（parse-only）时能声明：接口层规定「给了 options
  // 就必须给 source_dir」，所以源码目录留空时该选择不生效。
  const hasSourceDir = sourceDir !== '';

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();
    setSubmitting(true);
    setError(null);

    try {
      // 留空即省略 `options`：后端从官方 jar 跑完整五段；给 `options` 但 `source_dir` 为空会 422。
      await createJob(
        hasSourceDir
          ? {
              kind: 'index',
              version,
              options: {
                source_dir: sourceDir,
                // 不声明时省略该字段：由服务端按 checkpoint 推断（parse-only 缺省按「已 remap」）。
                ...(obfuscated === 'unset' ? {} : { obfuscated: obfuscated === 'remapped' }),
                force: false,
              },
            }
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

          <TextField
            select
            label="源码是否经官方映射重映射（remap）"
            value={obfuscated}
            onChange={(event) => setObfuscated(event.target.value as ObfuscatedChoice)}
            disabled={!hasSourceDir}
            helperText={
              hasSourceDir
                ? '不声明 = 由服务端推断（parse-only 缺省按「已 remap」记录）；26.x 这类不混淆的版本选「未 remap」'
                : '仅当填写源码目录（parse-only 任务）时可声明；完整五段任务由 download 阶段自动判定'
            }
            fullWidth
          >
            <MenuItem value="unset">不声明</MenuItem>
            <MenuItem value="remapped">已 remap</MenuItem>
            <MenuItem value="not-remapped">未 remap</MenuItem>
          </TextField>

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
