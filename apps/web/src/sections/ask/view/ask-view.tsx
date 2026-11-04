import type { AskEvent, AskReason, AskUsage, Citation, ConversationDetail } from 'src/lib/api-client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Collapse from '@mui/material/Collapse';
import Divider from '@mui/material/Divider';
import Drawer from '@mui/material/Drawer';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';

import { useConversations, useVersions } from 'src/hooks';

import { ApiError, deleteConversation, getConversation, streamAsk } from 'src/lib/api-client';

import { AnswerMarkdown } from './answer-markdown';
import { CitationDrawer } from './citation-drawer';
import { ConversationList } from './conversation-list';

// ----------------------------------------------------------------------

/** 左栏宽度（宽屏常驻）。 */
const SIDEBAR_WIDTH = 260;
/** URL 里当前会话的参数名（`?c=<id>`），刷新与分享链接靠它回到同一个会话。 */
const CONVERSATION_PARAM = 'c';

/** 工具轨迹里的一步：调用前只有 `args`，返回后补上 `ok` / `hits`；`context_budget` 只有 `args`。 */
type StepTrace = {
  tool: string;
  args?: Record<string, unknown>;
  ok?: boolean;
  hits?: number;
};

type Turn = {
  question: string;
  /** 该轮提问用的版本；历史会话载入的轮次为 `''`（用当前选中的版本打开引用）。 */
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

/** 历史会话 → 消息流：用户问题 + 紧跟的助手答案配成一轮（历史里没有工具轨迹）。 */
function turnsFromDetail(detail: ConversationDetail): Turn[] {
  const turns: Turn[] = [];
  const messages = detail.messages;

  for (let index = 0; index < messages.length; index += 1) {
    const message = messages[index];

    if (message.role !== 'user') continue;

    const next = messages[index + 1];
    const assistant = next?.role === 'assistant' ? next : null;

    turns.push({
      question: message.content,
      version: '',
      steps: [],
      answer: assistant?.content ?? '',
      citations: assistant?.citations ?? [],
      reason: null,
      usage: assistant?.usage ?? null,
      status: 'done',
      error: null,
    });

    if (assistant) index += 1;
  }

  return turns;
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

/**
 * `/ask` 聊天页：左栏历史会话 + 主区「输入框 → 工具轨迹 → 增量答案 → 可点击引用」。
 *
 * 当前会话以 `?c=<id>` 为准：刷新页面后左栏与主区都还在，链接也能分享给自己。
 * 首轮提问时后端自动建会话，`done` 事件回传 id，这里把它写进 URL（`displayed` ref 保证
 * 写 URL 不会反过来触发一次「载入历史」把刚流出来的内容冲掉）。
 */
export function AskView() {
  const { state: versionsState } = useVersions();
  const { conversations, loading: conversationsLoading, error: conversationsError, refresh } =
    useConversations();
  const [searchParams, setSearchParams] = useSearchParams();
  const conversationId = searchParams.get(CONVERSATION_PARAM);

  const [version, setVersion] = useState('');
  const [question, setQuestion] = useState('');
  const [busy, setBusy] = useState(false);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [loadingConversation, setLoadingConversation] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [navOpen, setNavOpen] = useState(false);
  // 抽屉的 `open` 与内容分开：关闭时保留内容，退场动画里不会闪空白。
  const [activeCitation, setActiveCitation] = useState<{
    citation: Citation;
    version: string;
  } | null>(null);
  const [drawerOpen, setDrawerOpen] = useState(false);
  // 主区当前展示的是哪个会话：写 URL 时先同步它，避免自己触发的「载入历史」。
  const displayed = useRef<string | null>(null);

  const versions = useMemo(
    () => (versionsState.status === 'ready' ? versionsState.versions : []),
    [versionsState]
  );

  // 版本必选（规格 §9）：默认第一个，不提供「全部版本」。
  useEffect(() => {
    if (!version && versions.length > 0) setVersion(versions[0].version);
  }, [version, versions]);

  // `?c=<id>` 变化时载入该会话；主区已经是它（首轮提问刚写入 URL）就跳过。
  useEffect(() => {
    if (conversationId === null) {
      if (displayed.current !== null) {
        displayed.current = null;
        setTurns([]);
      }

      return undefined;
    }

    if (conversationId === displayed.current) return undefined;

    let cancelled = false;
    displayed.current = conversationId;
    setLoadingConversation(true);
    setLoadError(null);

    getConversation(conversationId)
      .then((detail) => {
        if (!cancelled) setTurns(turnsFromDetail(detail));
      })
      .catch((cause: unknown) => {
        if (!cancelled) setLoadError(errorText(cause));
      })
      .finally(() => {
        if (!cancelled) setLoadingConversation(false);
      });

    return () => {
      cancelled = true;
      // StrictMode 会「挂载 → 卸载 → 再挂载」把 effect 跑两遍：第一遍被取消时要把标记退回去，
      // 否则第二遍会以为「主区已经是这个会话」而跳过载入，主区永远空着（只有转圈）。
      if (displayed.current === conversationId) displayed.current = null;
    };
  }, [conversationId]);

  const openConversation = (id: string) => {
    setNavOpen(false);
    setSearchParams({ [CONVERSATION_PARAM]: id });
  };

  const startNewConversation = () => {
    displayed.current = null;
    setTurns([]);
    setLoadError(null);
    setNavOpen(false);
    setSearchParams({});
  };

  const removeConversation = async (id: string) => {
    try {
      await deleteConversation(id);

      if (id === conversationId) startNewConversation();

      refresh();
    } catch (cause) {
      setLoadError(errorText(cause));
    }
  };

  const handleSubmit = async (event: React.FormEvent) => {
    event.preventDefault();

    if (busy || version === '' || question.trim() === '') return;

    const asked = question.trim();
    const askedVersion = version;
    const index = turns.length;
    let activeConversation = conversationId;

    setQuestion('');
    setBusy(true);
    setLoadError(null);
    setTurns((current) => [...current, emptyTurn(asked, askedVersion)]);

    try {
      await streamAsk(
        {
          question: asked,
          version: askedVersion,
          // 已有会话就续聊；没有就留空，后端首次提问时自动建会话。
          conversation_id: activeConversation ?? undefined,
          // `max_steps` / `max_answer_tokens` 不传：用服务端设置（设置页可改）。
        },
        (incoming) => {
          if (incoming.type === 'done' && activeConversation === null) {
            activeConversation = incoming.conversation_id;
            displayed.current = activeConversation;
            setSearchParams({ [CONVERSATION_PARAM]: activeConversation }, { replace: true });
          }

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
          position === index ? { ...turn, status: 'error', error: errorText(cause) } : turn
        )
      );
    } finally {
      setBusy(false);
      refresh();
    }
  };

  const sidebar = (
    <ConversationList
      conversations={conversations}
      activeId={conversationId}
      loading={conversationsLoading}
      error={conversationsError}
      onSelect={openConversation}
      onCreate={startNewConversation}
      onDelete={removeConversation}
      onRefresh={refresh}
    />
  );

  return (
    <Stack spacing={3}>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
        <Button
          size="small"
          sx={{ display: { xs: 'inline-flex', md: 'none' } }}
          onClick={() => setNavOpen(true)}
        >
          会话
        </Button>

        <Typography variant="h4" sx={{ flex: '1 1 auto' }}>
          问答
        </Typography>

        <Button
          size="small"
          sx={{ display: { xs: 'none', md: 'inline-flex' } }}
          onClick={() => setSidebarOpen((open) => !open)}
        >
          {sidebarOpen ? '隐藏会话' : '显示会话'}
        </Button>
      </Stack>

      <Stack direction="row" spacing={2} sx={{ alignItems: 'flex-start' }}>
        {sidebarOpen && (
          <Box
            sx={{
              display: { xs: 'none', md: 'block' },
              width: SIDEBAR_WIDTH,
              flexShrink: 0,
              position: 'sticky',
              top: 16,
              maxHeight: 'calc(100vh - 32px)',
            }}
          >
            {sidebar}
          </Box>
        )}

        <Drawer
          anchor="left"
          open={navOpen}
          onClose={() => setNavOpen(false)}
          sx={{ display: { md: 'none' } }}
        >
          <Box sx={{ width: 280, height: '100%', p: 2 }}>{sidebar}</Box>
        </Drawer>

        <Stack spacing={3} sx={{ flex: '1 1 auto', minWidth: 0 }}>
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

          {loadingConversation && (
            <Stack sx={{ alignItems: 'center', py: 2 }}>
              <CircularProgress size={24} />
            </Stack>
          )}

          {loadError !== null && <Alert severity="error">{loadError}</Alert>}

          {turns.map((turn, index) => (
            <TurnBlock
              key={`${index}:${turn.question}`}
              turn={turn}
              onOpenCitation={(citation) => {
                // 历史轮次没记版本，用当前选中的版本打开（见 `turnsFromDetail`）。
                setActiveCitation({ citation, version: turn.version || version });
                setDrawerOpen(true);
              }}
            />
          ))}
        </Stack>
      </Stack>

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
