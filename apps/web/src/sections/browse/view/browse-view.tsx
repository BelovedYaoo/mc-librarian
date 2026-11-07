import type {
  FileSlice,
  Hit,
  HitChannel,
  Outline,
  OutlineClass,
  OutlineMethod,
  SearchResponse,
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
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Switch from '@mui/material/Switch';
import FormControlLabel from '@mui/material/FormControlLabel';
import Typography from '@mui/material/Typography';

import { useVersions } from 'src/hooks';

import { ApiError, getOutline, getSymbol, readFile, search } from 'src/lib/api-client';

// ----------------------------------------------------------------------

const KIND_OPTIONS: { value: SymbolKind; label: string }[] = [
  { value: 'any', label: '全部' },
  { value: 'class', label: '类' },
  { value: 'method', label: '方法' },
  { value: 'field', label: '字段' },
];

/** `why` 的通道标签与配色（规格 §4 的固定顺序；`patch` 由 issue #24 追加）。 */
const CHANNEL_LABELS: Record<HitChannel, string> = {
  summary: '摘要',
  symbol: '符号',
  literal: '字面量',
  reference: '引用',
  patch: '补丁',
};

const CHANNEL_COLORS: Record<HitChannel, 'success' | 'primary' | 'warning' | 'secondary'> = {
  summary: 'success',
  symbol: 'primary',
  literal: 'warning',
  reference: 'secondary',
  patch: 'warning',
};

function errorText(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : String(cause);
}

// ----------------------------------------------------------------------

/** 检索调试台：混合检索 → 融合结果（why / score / lines）→ 展开片段 → 骨架与源码。 */
export function BrowseView() {
  const { state: versionsState } = useVersions();

  const [version, setVersion] = useState('');
  const [query, setQuery] = useState('');
  // 智能检索：扩展召回 + LLM 精排（较慢）。默认关，保持毫秒级的纯字面检索。
  const [smart, setSmart] = useState(false);
  const [pathPrefix, setPathPrefix] = useState('');
  const [kind, setKind] = useState<SymbolKind>('any');
  const [detail, setDetail] = useState<'snippet' | 'locate'>('snippet');
  const [limit, setLimit] = useState(10);
  const [maxTokens, setMaxTokens] = useState(4000);

  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [results, setResults] = useState<SearchResponse | null>(null);

  const [selected, setSelected] = useState<Hit | null>(null);
  const [symbolDetail, setSymbolDetail] = useState<SymbolDetail | null>(null);
  const [outline, setOutline] = useState<Outline | null>(null);
  const [detailError, setDetailError] = useState<string | null>(null);

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
    setSymbolDetail(null);
    setOutline(null);
    setDetailError(null);
    setSlice(null);
    setSliceError(null);
  };

  const runSearch = async (text: string) => {
    if (!version || text.trim() === '') return;

    setLoading(true);
    setError(null);
    setResults(null);
    resetDetail();

    try {
      const body = await search({
        query: text,
        version,
        mode: smart ? 'smart' : 'lexical',
        kind,
        detail,
        limit,
        max_tokens: maxTokens,
        path_prefix: pathPrefix.trim() === '' ? undefined : pathPrefix.trim(),
      });

      setResults(body);
      setQuery(text);
    } catch (cause) {
      setError(errorText(cause));
    } finally {
      setLoading(false);
    }
  };

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    void runSearch(query);
  };

  /** 建议可点击：追加到当前 query 作为新查询（规格 §11）。 */
  const handleSuggestion = (suggestion: string) => {
    const next = query.trim() === '' ? suggestion : `${query.trim()} ${suggestion}`;

    void runSearch(next);
  };

  const handleSelect = async (hit: Hit) => {
    setSelected(hit);
    setSymbolDetail(null);
    setOutline(null);
    setDetailError(null);
    setSlice(null);
    setSliceError(null);

    // 补丁命中的 `path` 是补丁文件（不在 `<data_dir>/src/<version>/` 里）：`/v1/outline` 与
    // `/v1/read` 都打不开它，片段已在 `hit.snippet`（hunk 正文）里，直接跳过骨架与切片，
    // 别弹一个注定 404 的错误（issue #24 的 `Hit.type = patch`）。
    if (hit.type === 'patch') {
      return;
    }

    if (hit.symbol) {
      try {
        const body = await getSymbol({ version, name: hit.symbol, fuzzy: false });

        setSymbolDetail(body.matches[0] ?? null);
      } catch (cause) {
        setDetailError(errorText(cause));
      }
    }

    try {
      setOutline(await getOutline({ version, path: hit.path }));
    } catch (cause) {
      setDetailError(errorText(cause));
    }
  };

  const handleRead = async (path: string, start: number, end: number) => {
    setSlice(null);
    setSliceError(null);

    try {
      setSlice(await readFile({ version, path, start, end }));
    } catch (cause) {
      setSliceError(errorText(cause));
    }
  };

  return (
    <Stack spacing={3}>
      <Typography variant="h4">检索调试台</Typography>

      <Card>
        <CardContent>
          <Box component="form" onSubmit={handleSubmit}>
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
                  label="查询"
                  value={query}
                  onChange={(event) => setQuery(event.target.value)}
                  placeholder="刷怪上限 / MobCategory / spawnInterval"
                  helperText="三路召回（符号 / 字面量 / 引用）在服务端融合"
                  fullWidth
                />
              </Stack>

              <Stack direction={{ xs: 'column', sm: 'row' }} spacing={2}>
                <TextField
                  select
                  label="详情"
                  value={detail}
                  onChange={(event) => setDetail(event.target.value as 'snippet' | 'locate')}
                  sx={{ minWidth: 140 }}
                >
                  <MenuItem value="snippet">片段（snippet）</MenuItem>
                  <MenuItem value="locate">仅定位（locate）</MenuItem>
                </TextField>

                <TextField
                  select
                  label="符号类型"
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

                <TextField
                  label="路径前缀"
                  value={pathPrefix}
                  onChange={(event) => setPathPrefix(event.target.value)}
                  placeholder="net/minecraft/world"
                  sx={{ flex: '1 1 auto' }}
                />

                <TextField
                  label="条数上限"
                  type="number"
                  value={limit}
                  onChange={(event) => setLimit(Number(event.target.value))}
                  sx={{ width: 120 }}
                />

                <TextField
                  label="token 预算"
                  type="number"
                  value={maxTokens}
                  onChange={(event) => setMaxTokens(Number(event.target.value))}
                  sx={{ width: 140 }}
                />
              </Stack>

              <Stack direction="row" spacing={2} sx={{ alignItems: 'center' }}>
                <FormControlLabel
                  control={
                    <Switch checked={smart} onChange={(event) => setSmart(event.target.checked)} />
                  }
                  label="智能检索"
                />
                <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                  先让模型改写查询再召回，并从真实候选里挑最相关的 5 条；慢 5–10 秒、额外消耗 token
                </Typography>

                <Box sx={{ flex: '1 1 auto' }} />

                <Button
                  type="submit"
                  variant="contained"
                  disabled={loading || !version || query.trim() === ''}
                >
                  {loading ? '检索中…' : '检索'}
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

      {error && <Alert severity="error">检索失败：{error}</Alert>}

      {results && results.hits.length === 0 && (
        <Alert severity="warning">没有命中（total 0）。换一个 query 或放宽路径前缀。</Alert>
      )}

      {results && results.hits.length > 0 && (
        <Card>
          <CardContent>
            <Typography variant="h6" sx={{ mb: 1 }}>
              命中（{results.hits.length} / total {results.total}）
            </Typography>

            <Typography variant="body2" sx={{ color: 'text.secondary', mb: 2 }}>
              耗时 {results.took_ms} ms · detail={detail}
            </Typography>

            {(results.suggestions?.length ?? 0) > 0 && (
              <Stack
                direction="row"
                spacing={1}
                sx={{ alignItems: 'center', mb: 2, flexWrap: 'wrap' }}
              >
                <Typography variant="caption" sx={{ color: 'text.secondary' }}>
                  建议
                </Typography>

                {(results.suggestions ?? []).map((suggestion) => (
                  <Chip
                    key={suggestion}
                    size="small"
                    variant="outlined"
                    label={suggestion}
                    onClick={() => handleSuggestion(suggestion)}
                  />
                ))}
              </Stack>
            )}

            <Stack spacing={2} divider={<Divider flexItem />}>
              {results.hits.map((hit) => (
                <HitRow
                  key={`${hit.id}:${hit.lines.join('-')}`}
                  hit={hit}
                  selected={selected === hit}
                  onSelect={() => void handleSelect(hit)}
                />
              ))}
            </Stack>
          </CardContent>
        </Card>
      )}

      {detailError && <Alert severity="error">读取详情失败：{detailError}</Alert>}

      {symbolDetail && selected && <SymbolDetailBlock detail={symbolDetail} />}

      {outline && selected && (
        <Card>
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

function HitRow({
  hit,
  selected,
  onSelect,
}: {
  hit: Hit;
  selected: boolean;
  onSelect: () => void;
}) {
  const [expanded, setExpanded] = useState(false);

  return (
    <Stack spacing={1}>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
        {hit.why.map((channel) => (
          <Chip
            key={channel}
            size="small"
            color={CHANNEL_COLORS[channel]}
            label={CHANNEL_LABELS[channel]}
          />
        ))}

        <Chip size="small" variant="outlined" label={hit.type} />

        <Typography
          variant="body2"
          sx={{
            fontFamily: 'monospace',
            flex: '1 1 auto',
            cursor: 'pointer',
            '&:hover': { textDecoration: 'underline' },
          }}
          onClick={onSelect}
        >
          {hit.symbol ?? hit.path}
        </Typography>

        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          {hit.path}:{hit.lines.join('-')}
          {hit.target ? ` → ${hit.target}` : ''}
        </Typography>

        <Chip size="small" variant="outlined" label={`score ${hit.score.toFixed(3)}`} />

        {hit.snippet && (
          <Button size="small" onClick={() => setExpanded((value) => !value)}>
            {expanded ? '收起片段' : '展开片段'}
          </Button>
        )}
      </Stack>

      {selected && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          已选中 · tokens {hit.tokens ?? 0}
        </Typography>
      )}

      {expanded && hit.snippet && (
        <Box
          component="pre"
          sx={{
            fontFamily: 'monospace',
            fontSize: 13,
            lineHeight: 1.6,
            overflowX: 'auto',
            bgcolor: 'action.hover',
            borderRadius: 1,
            p: 2,
            m: 0,
          }}
        >
          {hit.snippet}
        </Box>
      )}
    </Stack>
  );
}

function SymbolDetailBlock({ detail }: { detail: SymbolDetail }) {
  return (
    <Card>
      <CardContent>
        <Typography variant="h6" sx={{ fontFamily: 'monospace' }}>
          {detail.signature ?? detail.fqn}
        </Typography>

        <Typography variant="body2" sx={{ color: 'text.secondary', mb: 2 }}>
          {detail.kind} · {detail.path}:{detail.lines.join('-')}
          {detail.superclass ? ` · extends ${detail.superclass}` : ''}
          {detail.interfaces && detail.interfaces.length > 0
            ? ` · implements ${detail.interfaces.join(', ')}`
            : ''}
        </Typography>

        {(detail.annotations?.length ?? 0) > 0 && (
          <Stack direction="row" spacing={1} sx={{ mb: 2, flexWrap: 'wrap' }}>
            {(detail.annotations ?? []).map((annotation) => (
              <Chip key={annotation} size="small" variant="outlined" label={`@${annotation}`} />
            ))}
          </Stack>
        )}

        <Stack spacing={0.5}>
          {(detail.members ?? []).map((member) => (
            <Typography
              key={`${member.kind}-${member.name}-${member.lines?.join('-')}`}
              variant="body2"
              sx={{ fontFamily: 'monospace' }}
            >
              {member.kind === 'method' ? 'method' : 'field'} · {member.signature ?? member.name}
            </Typography>
          ))}
        </Stack>
      </CardContent>
    </Card>
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
    <Card>
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
                  // 用 `text.secondary` 而不是 `text.disabled`：行号是元信息，白底上
                  // `text.disabled`（#919EAB）只有 2.73:1，读起来太淡。
                  color: 'text.secondary',
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
