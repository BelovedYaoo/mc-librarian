import type { AskEvent, AskReason, AskUsage, Citation, ConversationDetail } from 'src/lib/api-client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Divider from '@mui/material/Divider';
import Drawer from '@mui/material/Drawer';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Tooltip from '@mui/material/Tooltip';
import Typography from '@mui/material/Typography';

import { useConversations, useVersions } from 'src/hooks';

import { ApiError, deleteConversation, getConversation, streamAsk } from 'src/lib/api-client';

import { AnswerMarkdown } from './answer-markdown';
import { CitationDrawer } from './citation-drawer';
import { ConversationList } from './conversation-list';
import { GeneratingIndicator } from './generating-indicator';
import { ThinkingTrace, type StepTrace } from './thinking-trace';

// ----------------------------------------------------------------------

/** 左栏宽度（宽屏常驻）。 */
const SIDEBAR_WIDTH = 260;
/**
 * 输入区三个控件（版本下拉 / 问题输入 / 提问按钮）的统一高度。
 *
 * 此前三者各用各的默认值：`size="small"` 的下拉 40px、带 label 的多行输入 56px、
 * 默认按钮 36.5px——并排时底边参差。这里用一个常量钉死；输入框用 `minHeight` 而不是
 * `height`，所以多行内容仍能把输入区撑高（聊天输入本来就该长）。
 */
const COMPOSER_CONTROL_HEIGHT = 44;
/** 距底多少像素以内算「用户还在底部」：只有这时新内容才自动滚到底。 */
const STICK_TO_BOTTOM_PX = 120;
/** URL 里当前会话的参数名（`?c=<id>`），刷新与分享链接靠它回到同一个会话。 */
const CONVERSATION_PARAM = 'c';

/**
 * 空态的示例问题（硬编码，不读后端）。
 *
 * 取自评测集 `apps/api/eval/questions.json`，只挑机制 / 数值 / 定位三类的通用题：
 * 机制 mech-01/mech-02、数值 num-01/num-05、定位 loc-01/loc-02；不含「版本差异」那类
 * （空态下还没选版本，跨版本问题会把新用户带沟里）。
 */
const EXAMPLE_QUESTIONS = [
  '刷怪上限是怎么算的',
  '随机刻是怎么驱动作物生长的',
  '默认的 mob cap 是多少',
  '玩家的基础移动速度是多少',
  '处理甘蔗生长的类在哪',
  '解析掉落表 JSON 的类在哪',
];

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
  /** 本轮请求开始的时间戳：轨迹与生成指示器的计时起点。 */
  startedAt: number | null;
};

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
    startedAt: Date.now(),
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
      // 每轮记下当时用的版本（迁移 v10 之前落库的历史行为 null → 回退到当前选择）。
      version: assistant?.version ?? '',
      steps: [],
      answer: assistant?.content ?? '',
      citations: assistant?.citations ?? [],
      reason: null,
      usage: assistant?.usage ?? null,
      status: 'done',
      error: null,
      // 历史轮次不计时：它们的耗时在 `usage.latency_ms` 里，轨迹也用服务端给的总值。
      startedAt: null,
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
 * `/ask` 聊天页：顶栏 + 左栏历史会话 + 右侧「消息区（内部滚动）+ 输入区（钉底）」。
 *
 * 页面自己占满视口高度且不滚动（外壳 `MainLayout maxWidth={false}`），滚动条只出现在左栏与
 * 消息区内部；新内容只在用户已贴着底（< 120px）时自动跟随，往上翻历史不会被拽回去。
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
  const messagesRef = useRef<HTMLDivElement | null>(null);
  const questionInputRef = useRef<HTMLTextAreaElement | null>(null);
  // 是否跟随新内容滚到底：用户往上翻历史时置 false，滚回底部附近再置 true。
  const stickToBottom = useRef(true);

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
        if (cancelled) return;

        // 换会话时从头看：重新贴底，别沿用上一个会话里「用户翻到中间」的状态。
        stickToBottom.current = true;
        setTurns(turnsFromDetail(detail));
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
    stickToBottom.current = true;
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

  /**
   * 输入框的键盘约定：`Enter` 提交、`Shift+Enter` 换行。
   *
   * 中文输入法组合期间（拼音还在候选框里）按 Enter 是「上屏选词」，`isComposing` 为 true，
   * 必须先放行给输入法——否则中文用户选词就误提交。
   */
  const handleQuestionKeyDown = (
    event: React.KeyboardEvent<HTMLInputElement | HTMLTextAreaElement>
  ) => {
    if (event.key !== 'Enter' || event.shiftKey) return;
    if (event.nativeEvent.isComposing) return;

    event.preventDefault();
    void submit();
  };

  /** 点示例问题：填进输入框并聚焦，让用户可以改完再发。 */
  const pickExample = (example: string) => {
    setQuestion(example);
    questionInputRef.current?.focus();
  };

  /** 只有用户还在底部附近（< 120px）才跟随新内容；往上翻看历史时不得被拽回底部。 */
  const handleMessagesScroll = () => {
    const element = messagesRef.current;

    if (element === null) return;

    stickToBottom.current =
      element.scrollHeight - element.scrollTop - element.clientHeight < STICK_TO_BOTTOM_PX;
  };

  useEffect(() => {
    if (!stickToBottom.current) return;

    const element = messagesRef.current;

    if (element !== null) element.scrollTop = element.scrollHeight;
  }, [turns]);

  const submit = async () => {
    if (busy || version === '' || question.trim() === '') return;

    const asked = question.trim();
    const askedVersion = version;
    const index = turns.length;
    let activeConversation = conversationId;

    setQuestion('');
    setBusy(true);
    setLoadError(null);
    // 自己发的问题必须看到：无论刚才翻到哪儿，都滚到底。
    stickToBottom.current = true;
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

  const handleSubmit = (event: React.FormEvent) => {
    event.preventDefault();
    void submit();
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
    <Box sx={{ display: 'flex', flexDirection: 'column', flex: '1 1 auto', minHeight: 0 }}>
      <Stack
        direction="row"
        spacing={1}
        sx={{
          flexShrink: 0,
          alignItems: 'center',
          px: { xs: 2, md: 3 },
          py: 1.5,
          borderBottom: '1px solid',
          borderColor: 'divider',
        }}
      >
        <Typography variant="h6" sx={{ flex: '1 1 auto' }}>
          问答
        </Typography>

        <Button
          size="small"
          sx={{ display: { xs: 'inline-flex', md: 'none' } }}
          onClick={() => setNavOpen(true)}
        >
          会话
        </Button>

        <Button
          size="small"
          sx={{ display: { xs: 'none', md: 'inline-flex' } }}
          onClick={() => setSidebarOpen((open) => !open)}
        >
          {sidebarOpen ? '隐藏会话' : '显示会话'}
        </Button>
      </Stack>

      <Box sx={{ display: 'flex', flex: '1 1 auto', minHeight: 0 }}>
        {sidebarOpen && (
          <Box
            sx={{
              display: { xs: 'none', md: 'flex' },
              flexDirection: 'column',
              width: SIDEBAR_WIDTH,
              flexShrink: 0,
              minHeight: 0,
              p: 2,
              // 会话列表栏也是白底（同最左导航），与消息区靠右边框分隔。
              bgcolor: 'background.default',
              borderRight: '1px solid',
              borderColor: 'divider',
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

        <Box
          sx={{
            display: 'flex',
            flexDirection: 'column',
            flex: '1 1 auto',
            minWidth: 0,
            minHeight: 0,
          }}
        >
          {/* 消息区：只有它滚，页面不滚。`minHeight: 0` 必须有，否则 flex 子项不会收缩。 */}
          <Box
            ref={messagesRef}
            onScroll={handleMessagesScroll}
            sx={{
              flex: '1 1 auto',
              minHeight: 0,
              overflowY: 'auto',
              px: { xs: 2, md: 3 },
              py: 2,
              bgcolor: 'background.paper',
            }}
          >
            {loadingConversation && (
              <Stack sx={{ alignItems: 'center', py: 2 }}>
                <CircularProgress size={24} />
              </Stack>
            )}

            {loadError !== null && <Alert severity="error">{loadError}</Alert>}

            {turns.length === 0 && !loadingConversation ? (
              <EmptyState onPick={pickExample} />
            ) : (
              <Stack spacing={3}>
                {turns.map((turn, index) => (
                  <TurnBlock
                    key={`${index}:${turn.question}`}
                    turn={turn}
                    currentVersion={version}
                    onOpenCitation={(citation) => {
                      // 历史轮次按当时版本打开；版本未知（v10 之前落库）才用当前选中的版本。
                      setActiveCitation({ citation, version: turn.version || version });
                      setDrawerOpen(true);
                    }}
                  />
                ))}
              </Stack>
            )}
          </Box>

          {/* 输入区：钉底，不随消息滚动。版本选择器属于这里——它是「这次提问的选择」。 */}
          <Box
            component="form"
            onSubmit={handleSubmit}
            sx={{
              flexShrink: 0,
              px: { xs: 2, md: 3 },
              py: 2,
              bgcolor: 'background.paper',
              borderTop: '1px solid',
              borderColor: 'divider',
            }}
          >
            {versionsState.status === 'ready' && versions.length === 0 && (
              <Alert severity="info" sx={{ mb: 2 }}>
                还没有已索引版本。管理员在「版本」页提交一次索引任务后再回来。
              </Alert>
            )}

            {/* 版本选择器与输入框同一行：它原来是「标签 + 下拉 + 一行 helperText」，
                白占两行高度。现在收成一个紧凑下拉，「只在选中的版本上检索」挪进 Tooltip。 */}
            <Stack
              direction="row"
              spacing={1.5}
              sx={{
                alignItems: 'flex-end',
                flexWrap: { xs: 'wrap', sm: 'nowrap' },
              }}
            >
              {/* 版本串可以很长（`neoforge-21.1.265+1.21.1`），窄下拉会截断；
                  Tooltip 里带完整版本，hover 就能看全。 */}
              <Tooltip title={`${version}｜只在选中的版本上检索`}>
                <TextField
                  select
                  size="small"
                  label="版本"
                  value={version}
                  onChange={(event) => setVersion(event.target.value)}
                  disabled={versions.length === 0}
                  sx={{
                    width: { xs: '100%', sm: 240 },
                    flexShrink: 0,
                    '& .MuiOutlinedInput-root': { height: COMPOSER_CONTROL_HEIGHT },
                  }}
                >
                  {versions.map((item) => (
                    <MenuItem key={item.version} value={item.version}>
                      {item.version}
                    </MenuItem>
                  ))}
                </TextField>
              </Tooltip>

              {/* 这一行**不带浮动 label**：多行 TextField 的 label 会给容器加顶部留白，
                  实测把静止高度从 44 撑到 60，三个控件就再也对不齐。用途由 placeholder 说明，
                  `aria-label` 补无障碍名。 */}
              <TextField
                value={question}
                onChange={(event) => setQuestion(event.target.value)}
                placeholder="刷怪上限在哪定义？"
                multiline
                minRows={1}
                maxRows={8}
                inputRef={questionInputRef}
                slotProps={{
                  htmlInput: { onKeyDown: handleQuestionKeyDown, 'aria-label': '问题' },
                }}
                size="small"
                sx={{
                  flex: '1 1 240px',
                  minWidth: 0,
                  '& .MuiOutlinedInput-root': { minHeight: COMPOSER_CONTROL_HEIGHT },
                }}
              />

              <Button
                type="submit"
                variant="contained"
                disabled={busy || version === '' || question.trim() === ''}
                sx={{ flexShrink: 0, height: COMPOSER_CONTROL_HEIGHT }}
              >
                {busy ? '回答中…' : '提问'}
              </Button>
            </Stack>
          </Box>
        </Box>
      </Box>

      <CitationDrawer
        open={drawerOpen}
        citation={activeCitation?.citation ?? null}
        version={activeCitation?.version ?? ''}
        onClose={() => setDrawerOpen(false)}
      />
    </Box>
  );
}

// ----------------------------------------------------------------------

/** 空态：还没有任何消息时居中给一句说明 + 几个可点的示例问题（点了填进输入框并聚焦）。 */
function EmptyState({ onPick }: { onPick: (question: string) => void }) {
  return (
    <Stack
      spacing={3}
      sx={{
        minHeight: '100%',
        alignItems: 'center',
        justifyContent: 'center',
        textAlign: 'center',
        px: 2,
      }}
    >
      <Typography variant="body1" sx={{ color: 'text.secondary', maxWidth: 520 }}>
        先选版本，再提问。回答会带上源码位置，点引用可看整文件。
      </Typography>

      <Stack spacing={1.5} sx={{ alignItems: 'center' }}>
        <Typography variant="subtitle2" sx={{ color: 'text.secondary' }}>
          试试这些
        </Typography>

        <Box
          sx={{ display: 'flex', flexWrap: 'wrap', gap: 1, justifyContent: 'center', maxWidth: 640 }}
        >
          {EXAMPLE_QUESTIONS.map((example) => (
            <Button key={example} size="small" variant="outlined" onClick={() => onPick(example)}>
              {example}
            </Button>
          ))}
        </Box>
      </Stack>
    </Stack>
  );
}

// ----------------------------------------------------------------------

function TurnBlock({
  turn,
  currentVersion,
  onOpenCitation,
}: {
  turn: Turn;
  currentVersion: string;
  onOpenCitation: (citation: Citation) => void;
}) {
  return (
    <Stack spacing={2} divider={<Divider flexItem />}>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center', flexWrap: 'wrap' }}>
        <Typography variant="subtitle1" sx={{ fontWeight: 600 }}>
          问 · {turn.question}
        </Typography>

        <VersionBadge version={turn.version} current={currentVersion} />
      </Stack>

      {turn.steps.length > 0 && (
        <ThinkingTrace
          steps={turn.steps}
          streaming={turn.status === 'streaming'}
          startedAt={turn.startedAt}
          latencyMs={turn.usage?.latency_ms ?? null}
        />
      )}

      {/* 还没有任何增量文本时的等待态：像素格 + 实时秒数。一旦开始出字就交给光标，不叠两个动画。 */}
      {turn.status === 'streaming' && turn.answer === '' && turn.steps.every((s) => s.ok !== undefined) && (
        <GeneratingIndicator startedAt={turn.startedAt} />
      )}

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
          {turn.usage.completion_tokens ?? 0} · 耗时 {turn.usage.latency_ms ?? 0} ms
        </Typography>
      )}
    </Stack>
  );
}

/**
 * 这一轮的版本标记。
 *
 * 引用的 `path` 是版本相关的：历史轮次的版本与当前下拉框不一致时给出提示，让用户知道这轮问的
 * 是哪个版本（不偷偷用另一个版本打开文件）；版本未知（迁移 v10 之前落库的历史行）时明说会用
 * 当前选中的版本打开。
 */
function VersionBadge({ version, current }: { version: string; current: string }) {
  if (version === '') {
    return (
      <Chip
        size="small"
        variant="outlined"
        color="warning"
        label={current === '' ? '此轮版本未知' : `此轮版本未知，按当前选择的 ${current} 打开`}
      />
    );
  }

  if (version === current) return null;

  return <Chip size="small" variant="outlined" label={`此轮版本 ${version}`} />;
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
