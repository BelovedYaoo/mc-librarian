import type {
  FileSlice,
  Outline,
  OutlineClass,
  OutlineMethod,
  SymbolDetail,
  SymbolKind,
} from 'src/lib/api-client';

import { useEffect, useMemo, useState } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import Divider from '@mui/material/Divider';
import FormControlLabel from '@mui/material/FormControlLabel';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import Switch from '@mui/material/Switch';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';

import { useVersions } from 'src/hooks';

import { ApiError, getOutline, getSymbol, readFile } from 'src/lib/api-client';

// ----------------------------------------------------------------------

const KIND_OPTIONS: { value: SymbolKind; label: string }[] = [
  { value: 'any', label: '全部' },
  { value: 'class', label: '类' },
  { value: 'method', label: '方法' },
  { value: 'field', label: '字段' },
];

/** 与服务端四级降级一一对应（规格 §6）；服务端已按级别排序，这里只负责标注与分组。 */
const LEVEL_LABELS = ['全限定名精确', '简单名精确', '后缀匹配', '模糊匹配'];

type MatchGroup = { level: number; matches: SymbolDetail[] };

function simpleName(match: SymbolDetail): string {
  const parts = match.kind === 'class' ? match.fqn.split('.') : match.fqn.split('#');

  return parts[parts.length - 1] ?? match.fqn;
}

function matchLevel(match: SymbolDetail, query: string): number {
  if (match.fqn === query) return 0;
  if (simpleName(match) === query) return 1;
  if (match.fqn.endsWith(`.${query}`)) return 2;

  return 3;
}

function groupByLevel(matches: SymbolDetail[], query: string): MatchGroup[] {
  const groups: MatchGroup[] = [];

  matches.forEach((match) => {
    const level = matchLevel(match, query);
    const last = groups[groups.length - 1];

    if (last && last.level === level) last.matches.push(match);
    else groups.push({ level, matches: [match] });
  });

  return groups;
}

// ----------------------------------------------------------------------

/** 检索调试台（基础版）：符号搜索 → 匹配列表 → 骨架 → 源码片段。 */
export function BrowseView() {
  const { state: versionsState } = useVersions();

  const [version, setVersion] = useState('');
  const [name, setName] = useState('');
  const [kind, setKind] = useState<SymbolKind>('any');
  const [fuzzy, setFuzzy] = useState(true);

  const [query, setQuery] = useState('');
  const [searching, setSearching] = useState(false);
  const [searchError, setSearchError] = useState<string | null>(null);
  const [matches, setMatches] = useState<SymbolDetail[]>([]);
  const [searched, setSearched] = useState(false);

  const [selected, setSelected] = useState<SymbolDetail | null>(null);
  const [outline, setOutline] = useState<Outline | null>(null);
  const [outlineError, setOutlineError] = useState<string | null>(null);

  const [slice, setSlice] = useState<FileSlice | null>(null);
  const [sliceError, setSliceError] = useState<string | null>(null);

  const versions = useMemo(
    () => (versionsState.status === 'ready' ? versionsState.versions : []),
    [versionsState]
  );

  useEffect(() => {
    if (!version && versions.length > 0) setVersion(versions[0].version);
  }, [version, versions]);

  const resetDetail = () => {
    setSelected(null);
    setOutline(null);
    setOutlineError(null);
    setSlice(null);
    setSliceError(null);
  };

  const handleSearch = async (event: React.FormEvent) => {
    event.preventDefault();
    setSearching(true);
    setSearchError(null);
    resetDetail();

    try {
      const body = await getSymbol({ version, name, fuzzy, kind });

      setMatches(body.matches);
      setQuery(name);
      setSearched(true);
    } catch (cause) {
      setSearchError(cause instanceof ApiError ? cause.message : String(cause));
      setMatches([]);
      setSearched(false);
    } finally {
      setSearching(false);
    }
  };

  const handleSelect = async (match: SymbolDetail) => {
    setSelected(match);
    setOutline(null);
    setOutlineError(null);
    setSlice(null);
    setSliceError(null);

    try {
      setOutline(await getOutline({ version, path: match.path }));
    } catch (cause) {
      setOutlineError(cause instanceof ApiError ? cause.message : String(cause));
    }
  };

  const handleRead = async (path: string, start: number, end: number) => {
    setSlice(null);
    setSliceError(null);

    try {
      setSlice(await readFile({ version, path, start, end }));
    } catch (cause) {
      setSliceError(cause instanceof ApiError ? cause.message : String(cause));
    }
  };

  return (
    <Stack spacing={3}>
      <Typography variant="h4">检索</Typography>

      <Card variant="outlined">
        <CardContent>
          <Box component="form" onSubmit={handleSearch}>
            <Stack spacing={2}>
              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                <TextField
                  select
                  label="版本"
                  value={version}
                  onChange={(event) => setVersion(event.target.value)}
                  sx={{ minWidth: 160 }}
                  disabled={versions.length === 0}
                >
                  {versions.map((item) => (
                    <MenuItem key={item.version} value={item.version}>
                      {item.version}
                    </MenuItem>
                  ))}
                </TextField>

                <TextField
                  label="符号名"
                  value={name}
                  onChange={(event) => setName(event.target.value)}
                  placeholder="MobCategory 或 net.minecraft.world.entity.MobCategory"
                  helperText="支持全限定名、简单名、后缀与模糊匹配"
                  required
                  fullWidth
                />

                <TextField
                  select
                  label="类型"
                  value={kind}
                  onChange={(event) => setKind(event.target.value as SymbolKind)}
                  sx={{ minWidth: 120 }}
                >
                  {KIND_OPTIONS.map((option) => (
                    <MenuItem key={option.value} value={option.value}>
                      {option.label}
                    </MenuItem>
                  ))}
                </TextField>
              </Stack>

              <Stack direction="row" spacing={2} sx={{ alignItems: 'center' }}>
                <FormControlLabel
                  control={
                    <Switch checked={fuzzy} onChange={(event) => setFuzzy(event.target.checked)} />
                  }
                  label="模糊匹配"
                />

                <Box sx={{ flex: '1 1 auto' }} />

                <Button
                  type="submit"
                  variant="contained"
                  disabled={searching || !version || name === ''}
                >
                  {searching ? '搜索中…' : '搜索'}
                </Button>
              </Stack>
            </Stack>
          </Box>
        </CardContent>
      </Card>

      {versionsState.status === 'ready' && versions.length === 0 && (
        <Alert severity="info">
          还没有已索引版本。管理员在「版本」页提交一次索引任务后再回来。
        </Alert>
      )}

      {searchError && <Alert severity="error">搜索失败：{searchError}</Alert>}

      {searched && matches.length === 0 && <Alert severity="warning">没有匹配的符号。</Alert>}

      {matches.length > 0 && (
        <Card variant="outlined">
          <CardContent>
            <Typography variant="h6" sx={{ mb: 2 }}>
              匹配（{matches.length}）
            </Typography>

            <Stack spacing={2} divider={<Divider flexItem />}>
              {groupByLevel(matches, query).map((group) => (
                <Stack key={group.level} spacing={1}>
                  <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                    {LEVEL_LABELS[group.level]}
                  </Typography>

                  {group.matches.map((match) => (
                    <MatchRow
                      key={match.fqn}
                      match={match}
                      selected={selected?.fqn === match.fqn}
                      onSelect={() => handleSelect(match)}
                    />
                  ))}
                </Stack>
              ))}
            </Stack>
          </CardContent>
        </Card>
      )}

      {outlineError && <Alert severity="error">读取骨架失败：{outlineError}</Alert>}

      {outline && selected && (
        <Card variant="outlined">
          <CardContent>
            <Typography variant="h6">骨架 · {outline.path}</Typography>

            <Typography variant="body2" sx={{ color: 'text.secondary', mb: 2 }}>
              package {outline.package ?? '（无）'}
              {outline.imports && outline.imports.length > 0
                ? ` · ${outline.imports.length} 个 import`
                : ''}
            </Typography>

            <Stack spacing={2} divider={<Divider flexItem />}>
              {outline.classes.map((outlineClass) => (
                <OutlineBlock
                  key={outlineClass.fqn ?? outlineClass.lines?.join('-')}
                  outlineClass={outlineClass}
                  path={outline.path}
                  onRead={handleRead}
                />
              ))}
            </Stack>
          </CardContent>
        </Card>
      )}

      {sliceError && <Alert severity="error">读取源码失败：{sliceError}</Alert>}

      {slice && <SliceBlock slice={slice} />}
    </Stack>
  );
}

// ----------------------------------------------------------------------

type MatchRowProps = {
  match: SymbolDetail;
  selected: boolean;
  onSelect: () => void;
};

function MatchRow({ match, selected, onSelect }: MatchRowProps) {
  return (
    <Stack
      direction="row"
      spacing={1.5}
      sx={{
        alignItems: 'center',
        cursor: 'pointer',
        borderRadius: 1,
        px: 1,
        py: 0.5,
        backgroundColor: selected ? 'action.selected' : undefined,
        '&:hover': { backgroundColor: 'action.hover' },
      }}
      onClick={onSelect}
    >
      <Chip size="small" label={match.kind} />
      <Typography variant="body2" sx={{ fontFamily: 'monospace', flex: '1 1 auto' }}>
        {match.fqn}
      </Typography>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        {match.path}:{match.lines.join('-')}
      </Typography>
    </Stack>
  );
}

type OutlineBlockProps = {
  outlineClass: OutlineClass;
  path: string;
  onRead: (path: string, start: number, end: number) => void;
};

function OutlineBlock({ outlineClass, path, onRead }: OutlineBlockProps) {
  return (
    <Stack spacing={1}>
      <Stack direction="row" spacing={1.5} sx={{ alignItems: 'center' }}>
        <Chip size="small" variant="outlined" label={outlineClass.kind ?? 'class'} />
        <Typography variant="subtitle2" sx={{ fontFamily: 'monospace' }}>
          {outlineClass.signature ?? outlineClass.fqn}
        </Typography>
      </Stack>

      <Stack spacing={0.5}>
        {(outlineClass.methods ?? []).map((method) => (
          <MethodRow
            key={`${method.name}-${method.lines?.join('-')}`}
            method={method}
            onRead={() => {
              const [start, end] = method.lines ?? [];

              if (start !== undefined && end !== undefined) {
                onRead(path, start, end);
              }
            }}
          />
        ))}
      </Stack>
    </Stack>
  );
}

function MethodRow({ method, onRead }: { method: OutlineMethod; onRead: () => void }) {
  return (
    <Stack
      direction="row"
      spacing={1.5}
      sx={{
        alignItems: 'center',
        cursor: 'pointer',
        borderRadius: 1,
        px: 1,
        '&:hover': { backgroundColor: 'action.hover' },
      }}
      onClick={onRead}
    >
      <Typography variant="body2" sx={{ fontFamily: 'monospace', flex: '1 1 auto' }}>
        {method.signature ?? method.name}
      </Typography>
      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        {method.lines?.join('-')}
      </Typography>
    </Stack>
  );
}

// ----------------------------------------------------------------------

function SliceBlock({ slice }: { slice: FileSlice }) {
  const lines = slice.content === '' ? [] : slice.content.split('\n');

  return (
    <Card variant="outlined">
      <CardContent>
        <Typography variant="h6">
          源码 · {slice.path}:{slice.start}-{slice.end}
        </Typography>

        <Typography variant="body2" sx={{ color: 'text.secondary', mb: 2 }}>
          共 {slice.total_lines ?? '?'} 行
          {slice.truncated ? '（超出 token 预算，已从尾部截断）' : ''}
        </Typography>

        <Box
          sx={{
            fontFamily: 'monospace',
            fontSize: 13,
            lineHeight: 1.6,
            overflowX: 'auto',
            bgcolor: 'action.hover',
            borderRadius: 1,
            p: 2,
          }}
        >
          {lines.map((line, index) => (
            <Box key={slice.start + index} sx={{ display: 'flex' }}>
              <Box
                component="span"
                sx={{
                  width: 48,
                  flexShrink: 0,
                  pr: 2,
                  textAlign: 'right',
                  color: 'text.disabled',
                  userSelect: 'none',
                }}
              >
                {slice.start + index}
              </Box>
              <Box component="span" sx={{ whiteSpace: 'pre' }}>
                {line === '' ? ' ' : line}
              </Box>
            </Box>
          ))}
        </Box>
      </CardContent>
    </Card>
  );
}
