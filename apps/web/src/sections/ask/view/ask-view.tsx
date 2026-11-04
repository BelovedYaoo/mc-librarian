import type { AskEvent, AskReason, AskUsage, Citation } from 'src/lib/api-client';

import { useEffect, useMemo, useState } from 'react';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import Collapse from '@mui/material/Collapse';
import Divider from '@mui/material/Divider';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';

import { useVersions } from 'src/hooks';

import { ApiError, streamAsk } from 'src/lib/api-client';

import { AnswerMarkdown } from './answer-markdown';
import { CitationDrawer } from './citation-drawer';

// ----------------------------------------------------------------------

/** 工具轨迹里的一步：调用前只有 `args`，返回后补上 `ok` / `hits`；`context_budget` 只有 `args`。 */
type StepTrace = {
  tool: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  hits?: number;
};

type Turn = {
  question: string;
  version: string;
  steps: StepTrace[];
  answer: string;
  citations: Citation[];
  reason: AskReason | null;
  usage: AskUsage | null;
  status: 'streaming' | 'done' | 'error';
  error: string | null;
};

const CONTEXT_BUDGET_TOOL = 'context_budget';

function errorText(cause: unknown): string {
  return cause instanceof ApiError ? cause.message : String(cause);
}

function emptyTurn(question: string, version: string): Turn {
  return {
    question,
    version,
    steps: [],
    answer: '',
    citations: [],
    reason: null,
    usage: null,
    status: 'streaming',
    error: null,
  };
}

/** 把一个 SSE 事件落到当前这一轮上（规格 §4 的 5 种事件）。 */
function applyEvent(turn: Turn, event: AskEvent): Turn {
  switch (event.type) {
    case 'step': {
      if (event.args !== undefined) {
        return { ...turn, steps: [...turn.steps, { tool: event.tool, args: event.args }] };
      }

      // 返回后的事件不带 args：补在最后一步还没收尾的同名工具上。
      const steps = [...turn.steps];

      for (let index = steps.length - 1; index >= 0; index -= 1) {
        if (steps[index].ok === undefined && steps[index].tool === event.tool) {
          steps[index] = { ...steps[index], ok: event.ok, hits: event.hits };
          break;
        }
      }

      return { ...turn, steps };
    }

    case 'delta':
      return { ...turn, answer: turn.answer + event.text };

    case 'citation':
      return {
        ...turn,
        citations: [
          ...turn.citations,
          { path: event.path, lines: event.lines, symbol: event.symbol },
        ],
      };

    case 'done':
      return { ...turn, reason: event.reason, usage: event.usage, status: 'done' };

    case 'error':
      return {
        ...turn,
        status: 'error',
        error: event.problem.detail ?? event.problem.title ?? '问答失败',
      };

    default:
      return turn;
  }
}

// ----------------------------------------------------------------------

/** `/ask` 聊天页：输入框 + 版本选择 → 工具轨迹 → 增量答案 → 可点击引用。 */
export function AskView() {
  const { state: versionsState } = useVersions();

  const [version, setVersion] = useState('');
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  // 抽屉的 `open` 与内容分开：关闭时保留内容，退场动画里不会闪空白。
  const [activeCitation, setActiveCitation] = useState<{
    citation: Citation;
    version: string;
  } | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);

  const versions = useMemo(
    () => (versionsState.status === 'ready' ? versionsState.versions : []),
    [versionsState]
  );

  // 版本必选（规格 §9）：默认第一个，不提供「全部版本」。
  useEffect(() => {
    if (!version && versions.length > 0) setVersion(versions[0].version);
  }, [version, versions]);

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();

    if (busy || version === '' || question.trim() === '') return;

    const asked = question.trim();
    const index = turns.length;

    setQuestion('');
    setBusy(true);
    setTurns((current) => [...current, emptyTurn(asked, version)]);

    try {
      await streamAsk(
        {
          question: asked,
          version,
          // `max_steps` / `max_answer_tokens` 不传：用服务端设置（设置页可改）。
        },
        (incoming) => {
          setTurns((current) =>
            current.map((turn, position) =>
              position === index ? applyEvent(turn, incoming) : turn
            )
          );
        }
      );

      setTurns((current) =>
        current.map((turn, position) =>
          position === index && turn.status === 'streaming' ? { ...turn, status: 'done' } : turn
        )
      );
    } catch (cause) {
      setTurns((current) =>
        current.map((turn, position) =>
          position === index
            ? { ...turn, status: 'error', error: errorText(cause) }
            : turn
        )
      );
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack spacing={3}>
      <Typography variant="h4">问答</Typography>

      <Card variant="outlined">
        <CardContent>
          <Box component="form" onSubmit={handleSubmit}>
            <Stack spacing={2}>
              <TextField
                select
                label="版本"
                value={version}
                onChange={(event) => setVersion(event.target.value)}
                sx={{ maxWidth: 220 }}
                disabled={versions.length === 0}
                helperText="问答只在单个版本上检索（不支持「全部版本」）"
              >
                {versions.map((item) => (
                  <MenuItem key={item.version} value={item.version}>
                    {item.version}
                  </MenuItem>
                ))}
              </TextField>

              <TextField
                label="问题"
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                placeholder="刷怪上限在哪定义？"
                multiline
                minRows={2}
                fullWidth
              />

              <Stack direction="row" spacing={2}>
                <Box sx={{ flex: '1 1 auto' }} />

                <Button
                  type="submit"
                  variant="contained"
                  disabled={busy || version === '' || question.trim() === ''}
                >
                  {busy ? '回答中…' : '提问'}
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

      {turns.map((turn, index) => (
        <TurnBlock
          key={`${index}:${turn.question}`}
          turn={turn}
          onOpenCitation={(citation) => {
            setActiveCitation({ citation, version: turn.version });
            setDrawerOpen(true);
          }}
        />
      ))}

      <CitationDrawer
        open={drawerOpen}
        citation={activeCitation?.citation ?? null}
        version={activeCitation?.version ?? ''}
        onClose={() => setDrawerOpen(false)}
      />
    </Stack>
  );
}

// ----------------------------------------------------------------------

function TurnBlock({
  turn,
  onOpenCitation,
}: {
  turn: Turn;
  onOpenCitation: (citation: Citation) => void;
}) {
  return (
    <Stack spacing={2} divider={<Divider flexItem />}>
      <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
        问 · {turn.question}
      </Typography>

      {turn.steps.length > 0 && <StepTraceBlock steps={turn.steps} />}

      {turn.status === 'error' && <Alert severity="error">问答失败：{turn.error}</Alert>}

      {turn.reason === 'degraded' && (
        <Alert severity="warning">模型暂不可用，以下是检索到的相关代码位置。</Alert>
      )}

      {turn.reason === 'max_steps' && (
        <Alert severity="info">已达检索步数上限，以下基于已获取的信息。</Alert>
      )}

      {turn.answer !== '' && (
        <AnswerMarkdown text={turn.answer} streaming={turn.status === 'streaming'} />
      )}

      {turn.citations.length > 0 && (
        <Stack spacing={1}>
          <Typography variant="subtitle2">引用（点击查看整个文件）</Typography>

          {turn.citations.map((citation, index) => (
            <CitationRow
              key={`${citation.path}:${citation.lines.join('-')}:${index}`}
              citation={citation}
              onOpen={() => onOpenCitation(citation)}
            />
          ))}
        </Stack>
      )}

      {turn.usage && turn.status === 'done' && (
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          用量：prompt {turn.usage.prompt_tokens ?? 0} · completion{' '}
          {turn.usage.completion_tokens ?? 0} · 成本 ${turn.usage.cost_usd ?? 0} · 耗时{' '}
          {turn.usage.latency_ms ?? 0} ms
        </Typography>
      )}
    </Stack>
  );
}

function StepTraceBlock({ steps }: { steps: StepTrace[] }) {
  const [expanded, setExpanded] = useState(false);
  // `context_budget` 是伪工具（只有 `args`，永远没有 `ok`）：它不进「完成/总数」的分母。
  const toolSteps = steps.filter((step) => step.tool !== CONTEXT_BUDGET_TOOL);
  const done = toolSteps.filter((step) => step.ok !== undefined).length;

  return (
    <Stack spacing={1}>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <Chip size="small" variant="outlined" label={`工具轨迹 ${done}/${toolSteps.length}`} />

        <Button size="small" onClick={() => setExpanded((value) => !value)}>
          {expanded ? '收起轨迹' : '展开轨迹'}
        </Button>
      </Stack>

      <Collapse in={expanded}>
        <Stack spacing={0.5}>
          {steps.map((step, index) => (
            <Typography
              key={`${step.tool}-${index}`}
              variant="body2"
              sx={{ fontFamily: 'monospace' }}
            >
              {stepLine(step)}
            </Typography>
          ))}
        </Stack>
      </Collapse>
    </Stack>
  );
}

/** 一步轨迹的展示文本；`context_budget` 没有 `ok`，单独渲染（不再落进「…」那一支）。 */
function stepLine(step: StepTrace): string {
  if (step.tool === CONTEXT_BUDGET_TOOL) {
    const dropped = droppedLabels(step.args);
    const detail = dropped.length > 0 ? `：${dropped.join('、')}` : '';

    return `${step.tool}（已丢弃 ${dropped.length} 条最旧结果${detail}）`;
  }

  const args = step.args === undefined ? '' : ` ${JSON.stringify(step.args)}`;

  if (step.ok === undefined) return `${step.tool}${args} …`;

  return `${step.tool}${args}${step.ok ? ` → ${step.hits ?? 0} 条` : ' → 失败'}`;
}

/** `context_budget` 的 `args.dropped`：被丢弃的工具结果标签（后端给字符串数组）。 */
function droppedLabels(args: Record<string, unknown> | undefined): string[] {
  const dropped = args?.dropped;

  return Array.isArray(dropped) ? dropped.map(String) : [];
}

/** 一条引用：点击后打开右侧抽屉，展示整个文件并高亮这几行（不跳走，不丢上下文）。 */
function CitationRow({ citation, onOpen }: { citation: Citation; onOpen: () => void }) {
  const lines = citation.lines;

  return (
    <Stack
      direction="row"
      spacing={1}
      sx={{
        alignItems: 'center',
        cursor: 'pointer',
        borderRadius: 1,
        px: 1,
        '&:hover': { backgroundColor: 'action.hover' },
      }}
      onClick={onOpen}
    >
      <Chip size="small" variant="outlined" label={citation.symbol ?? '代码位置'} />

      <Typography variant="body2" sx={{ fontFamily: 'monospace', flex: '1 1 auto' }}>
        {citation.path}:{lines.join('-')}
      </Typography>

      <Typography variant="caption" sx={{ color: 'text.secondary' }}>
        查看
      </Typography>
    </Stack>
  );
}
