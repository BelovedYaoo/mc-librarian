import type {
  AskEvent,
  AskReason,
  AskUsage,
  Citation,
  ConversationDetail,
} from 'src/lib/api-client';

import { useEffect, useMemo, useRef, useState } from 'react';
import { useSearchParams } from 'react-router';
import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Chip from '@mui/material/Chip';
import { alpha } from '@mui/material/styles';
import CircularProgress from '@mui/material/CircularProgress';
import Drawer from '@mui/material/Drawer';
import MenuItem from '@mui/material/MenuItem';
import Stack from '@mui/material/Stack';
import TextField from '@mui/material/TextField';
import Tooltip from '@mui/material/Tooltip';
import Typography from '@mui/material/Typography';

import { useConversations, useVersions } from 'src/hooks';

import {
  ApiError,
  deleteConversation,
  getConversation,
  streamAsk,
  streamConversation,
} from 'src/lib/api-client';

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
/** 引用列表默认显示几条（其余折叠；一次深度检索常有 20 条）。 */
const COLLAPSED_CITATIONS = 5;

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

/**
 * 一轮回答的**有序片段**。
 *
 * 模型会在工具调用之间插入正文（「我再查一下…」），随后继续调工具。只用一个 `steps[]` 加一个
 * `answer` 会把中间那几段正文挤到所有工具之后，用户看到的是「一堆工具 → 全部正文」，
 * 与真实过程不符。片段按到达顺序排列，渲染时原样展开。
 */
type Segment = { kind: 'steps'; steps: StepTrace[] } | { kind: 'text'; text: string };

type Turn = {
  question: string;
  /** 该轮提问用的版本；历史会话载入的轮次为 `''`（用当前选中的版本打开引用）。 */
  version: string;
  /** 有序片段（工具批次与正文交替）。 */
  segments: Segment[];
  /** 完整正文（各 `text` 片段拼起来）。落库与用量展示用它，渲染走 `segments`。 */
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

/**
 * 历史消息 → 渲染片段。
 *
 * 片段是**渲染**用的，`content` 才是完整正文；两者由同一份数据生成。服务端没给片段
 * （v13 之前落库的历史）时退化成一段正文——那时的轨迹本来就没存。
 */
function segmentsFromHistory(assistant: ConversationDetail['messages'][number] | null): Segment[] {
  if (!assistant) return [];

  const stored = assistant.segments;

  if (stored && stored.length > 0) {
    const merged: Segment[] = [];

    for (const segment of stored) {
      const last = merged[merged.length - 1];

      // 相邻的工具批次并成一块：服务端现在存前就合并，但 v13 期间存下的历史里
      // 是「一批一段」，不合并会显示成一叠「已调用 2 次工具」的卡片。
      if (segment.kind === 'steps') {
        if (last?.kind === 'steps') {
          last.steps.push(...segment.steps.map((step) => ({ ...step })));
        } else {
          merged.push({ kind: 'steps', steps: segment.steps.map((step) => ({ ...step })) });
        }

        continue;
      }

      merged.push({ kind: 'text', text: segment.text });
    }

    return merged;
  }

  return assistant.content === '' ? [] : [{ kind: 'text', text: assistant.content }];
}

function emptyTurn(question: string, version: string): Turn {
  return {
    question,
    version,
    segments: [],
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
      // 每轮记下当时用的版本。**用户消息上也存了版本**：助手消息要等生成结束才落库，
      // 刷新时若还在生成，就只有用户消息可读——只读助手消息会误判成「版本未知」
      // （生产上表现为「此轮版本未知，按当前选择的 X 打开」）。
      // 迁移 v10 之前落库的行为 null，才回退到当前选择。
      version: message.version ?? assistant?.version ?? '',
      // 服务端存了有序片段（v13+）就直接用：轨迹与正文的交替顺序能原样还原。
      // 更早的历史没有片段，退化成「整段正文」——那时的轨迹本来就没落库。
      segments: segmentsFromHistory(assistant),
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
        // 「开始」：接在本轮最后一个工具批次里；若最后一个片段是正文，说明中间插了话，
        // 从这里**另起一批**——这正是「工具 / 正文 / 工具」分开的依据。
        const last = turn.segments[turn.segments.length - 1];
        const step: StepTrace = { tool: event.tool, args: event.args };

        if (last?.kind === 'steps') {
          return {
            ...turn,
            segments: [
              ...turn.segments.slice(0, -1),
              { kind: 'steps', steps: [...last.steps, step] },
            ],
          };
        }

        return { ...turn, segments: [...turn.segments, { kind: 'steps', steps: [step] }] };
      }

      // 「完成」：补在当前批次里**最早**一步还没收尾的同名工具上。
      //
      // 一轮里的多个调用是并发跑的，服务端先为整批发「开始」再按请求顺序发「完成」——
      // 同一批里可能挂着多个同名工具（比如两个 `search`），必须按先来先配对；
      // 取最后一步会把 A 的结果挂到 B 上。
      const last = turn.segments[turn.segments.length - 1];

      if (last?.kind !== 'steps') return turn;

      const steps = [...last.steps];
      const target = steps.findIndex((step) => step.ok === undefined && step.tool === event.tool);

      if (target === -1) return turn;

      steps[target] = { ...steps[target], ok: event.ok, hits: event.hits, error: event.error };

      return { ...turn, segments: [...turn.segments.slice(0, -1), { kind: 'steps', steps }] };
    }

    case 'delta': {
      // 正文增量：接着上一段正文；若上一段是工具批次，就新起一段（正文插在工具之后）。
      const last = turn.segments[turn.segments.length - 1];
      const answer = turn.answer + event.text;

      if (last?.kind === 'text') {
        return {
          ...turn,
          answer,
          segments: [...turn.segments.slice(0, -1), { kind: 'text', text: last.text + event.text }],
        };
      }

      return { ...turn, answer, segments: [...turn.segments, { kind: 'text', text: event.text }] };
    }

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
  const {
    conversations,
    loading: conversationsLoading,
    error: conversationsError,
    refresh,
  } = useConversations();
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

    const attached = new AbortController();

    /** 只改「最后一轮」：接流时那一轮就是正在生成的这轮，不用记下标。 */
    const updateLastTurn = (update: (turn: Turn) => Turn) => {
      setTurns((current) =>
        current.map((turn, position) => (position === current.length - 1 ? update(turn) : turn))
      );
    };

    /** 接上仍在后端生成的那一轮（生成不依赖客户端连接，断开只是停止观看）。 */
    const follow = async (id: string) => {
      updateLastTurn((turn) => ({
        ...turn,
        status: 'streaming',
        startedAt: turn.startedAt ?? Date.now(),
      }));

      try {
        await streamConversation(
          id,
          (incoming) => updateLastTurn((turn) => applyEvent(turn, incoming)),
          attached.signal
        );

        updateLastTurn((turn) =>
          turn.status === 'streaming' ? { ...turn, status: 'done' } : turn
        );
      } catch (cause) {
        if (attached.signal.aborted) return; // 换会话 / 离开页面：不是错误
        updateLastTurn((turn) => ({ ...turn, status: 'error', error: errorText(cause) }));
      } finally {
        refresh();
      }
    };

    getConversation(conversationId)
      .then((detail) => {
        if (cancelled) return;

        // 换会话时从头看：重新贴底，别沿用上一个会话里「用户翻到中间」的状态。
        stickToBottom.current = true;
        setTurns(turnsFromDetail(detail));

        // 回答可能仍在后端生成（生成不依赖客户端连接）：接上去继续看，
        // 而不是显示成「问了一句、没有回答」。`attached` 在换会话/卸载时中断这条订阅。
        if (detail.running) void follow(conversationId);
      })
      .catch((cause: unknown) => {
        if (!cancelled) setLoadError(errorText(cause));
      })
      .finally(() => {
        if (!cancelled) setLoadingConversation(false);
      });

    return () => {
      cancelled = true;
      attached.abort();
      // StrictMode 会「挂载 → 卸载 → 再挂载」把 effect 跑两遍：第一遍被取消时要把标记退回去，
      // 否则第二遍会以为「主区已经是这个会话」而跳过载入，主区永远空着（只有转圈）。
      if (displayed.current === conversationId) displayed.current = null;
    };
    // `refresh` 只用来刷左栏列表，身份稳定（hook 内部 useCallback）；不放进依赖会让
    // exhaustive-deps 报警，但放进去又会因为它是每次渲染新函数而反复重载会话。
    // 这里显式声明依赖意图。
    // eslint-disable-next-line react-hooks/exhaustive-deps
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
          sx={{
            display: 'flex',
            flexWrap: 'wrap',
            gap: 1,
            justifyContent: 'center',
            maxWidth: 640,
          }}
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
  const streaming = turn.status === 'streaming';

  return (
    <Stack spacing={1.5}>
      {/* 用户气泡：靠右。不再用「问 · 」前缀——左右分栏本身就是角色区分。 */}
      <Stack direction="row" sx={{ justifyContent: 'flex-end' }}>
        <Box
          sx={{
            maxWidth: { xs: '92%', md: '76%' },
            px: 2,
            py: 1.25,
            borderRadius: 2.5,
            // 主色的低透明度：暗色下不刺眼，亮色下也能与助手气泡区分开。
            bgcolor: (theme) =>
              alpha(theme.palette.primary.main, theme.palette.mode === 'dark' ? 0.18 : 0.1),
            border: '1px solid',
            borderColor: (theme) => alpha(theme.palette.primary.main, 0.24),
          }}
        >
          <Typography variant="body1" sx={{ whiteSpace: 'pre-wrap' }}>
            {turn.question}
          </Typography>

          <Stack direction="row" spacing={1} sx={{ mt: 0.5, alignItems: 'center' }}>
            <VersionBadge version={turn.version} current={currentVersion} />
          </Stack>
        </Box>
      </Stack>

      {/* 助手气泡：靠左，占满宽度——答案里有表格与代码块，限宽会让它们频繁横向滚动。 */}
      <Box
        sx={{
          px: 2,
          py: 1.5,
          borderRadius: 2.5,
          border: '1px solid',
          borderColor: 'divider',
          bgcolor: 'background.paper',
          // 引用行里的长路径已经做了单行省略；这里再兜一层，任何子内容都不该把气泡撑宽。
          overflow: 'hidden',
        }}
      >
        <Stack spacing={1.5}>
          {/* 加载行**常驻**整轮：之前只在「没有工具在跑」时出现，于是每新增一次工具调用就
              闪一下（消失 → 出现）。现在这一行跟着 `streaming` 走，标签随阶段切换。 */}
          {streaming && (
            <GeneratingIndicator
              startedAt={turn.startedAt}
              label={turn.answer === '' ? '正在生成回答' : '正在输出答案'}
            />
          )}

          {/* 片段按到达顺序渲染：工具批次 → 正文 → 工具批次 → 正文。
              模型在两次调工具之间说的话属于正文，不该被挤到所有工具之后。 */}
          {turn.segments.map((segment, index) => {
            // 只有**最后一段**是「还在发生」的。中间的工具批次/正文都已经定稿：
            // 之前把 streaming 传给所有批次，导致第一批明明结束了还在转圈显示「正在规划下一步」。
            const isLast = index === turn.segments.length - 1;

            return segment.kind === 'steps' ? (
              <ThinkingTrace
                key={`steps-${index}`}
                steps={segment.steps}
                streaming={streaming && isLast}
                startedAt={turn.startedAt}
                // 总耗时只属于整轮，贴在最后一个批次上；中间批次不显示（否则每批都写同一个总数）。
                latencyMs={isLast ? (turn.usage?.latency_ms ?? null) : null}
              />
            ) : (
              <AnswerMarkdown
                key={`text-${index}`}
                text={segment.text}
                // 只有最后一段还在流式时才显示光标；中间段落已经定稿了。
                streaming={streaming && isLast}
                // 答案里写着 `Foo.java:12-34` 的位置可以点开源码（与底部引用同一个抽屉）；
                // 传入本轮引用，用于把「只有文件名」的裸引用补成完整路径。
                onOpenCitation={onOpenCitation}
                citations={turn.citations}
              />
            );
          })}

          {turn.status === 'error' && <Alert severity="error">问答失败：{turn.error}</Alert>}

          {turn.reason === 'degraded' && (
            <Alert severity="warning">模型暂不可用，以下是检索到的相关代码位置。</Alert>
          )}

          {/* 服务端在 `max_steps` 收尾时会把同一句提示**前缀进答案**，这里再放一条 Alert 就重复了。
              只在答案里没有那句话时才补（降级路径的答案不带前缀）。 */}
          {turn.reason === 'max_steps' && !turn.answer.includes('已达检索步数上限') && (
            <Alert severity="info">已达检索步数上限，以下基于已获取的信息。</Alert>
          )}

          {turn.citations.length > 0 && (
            <CitationList citations={turn.citations} onOpen={onOpenCitation} />
          )}

          {turn.usage && turn.status === 'done' && (
            <Typography variant="caption" sx={{ color: 'text.secondary' }}>
              用量：prompt {turn.usage.prompt_tokens ?? 0} · completion{' '}
              {turn.usage.completion_tokens ?? 0} · 耗时 {turn.usage.latency_ms ?? 0} ms
            </Typography>
          )}
        </Stack>
      </Box>
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

/**
 * 引用列表：默认只显示前 `COLLAPSED_CITATIONS` 条，其余折叠。
 *
 * 一次深度检索会带回 20 条引用（服务端 `MAX_CITATIONS`），全部铺开时一屏都是路径，
 * 把答案本身挤没了。折叠后只留最相关的前几条，需要时再展开。
 */
function CitationList({
  citations,
  onOpen,
}: {
  citations: Citation[];
  onOpen: (citation: Citation) => void;
}) {
  const [expanded, setExpanded] = useState(false);
  const hidden = citations.length - COLLAPSED_CITATIONS;
  const visible = expanded ? citations : citations.slice(0, COLLAPSED_CITATIONS);

  return (
    <Stack spacing={1}>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'baseline', flexWrap: 'wrap' }}>
        <Typography variant="subtitle2">引用 · {citations.length} 条</Typography>
        <Typography variant="caption" sx={{ color: 'text.secondary' }}>
          点击任意一条查看整个文件
        </Typography>
      </Stack>

      {visible.map((citation, index) => (
        <CitationRow
          key={`${citation.path}:${citation.lines.join('-')}:${index}`}
          citation={citation}
          onOpen={() => onOpen(citation)}
        />
      ))}

      {hidden > 0 && (
        <Box>
          <Button size="small" onClick={() => setExpanded((value) => !value)}>
            {expanded ? '收起引用' : `展开其余 ${hidden} 条`}
          </Button>
        </Box>
      )}
    </Stack>
  );
}

/** 一条引用：点击后打开右侧抽屉，展示整个文件并高亮这几行（不跳走，不丢上下文）。 */
/** 文件基名（去掉目录与后缀）：`a/b/C.java` → `C`，`a/b/D.java.patch` → `D`。 */
function fileStem(path: string): string {
  const base = path.slice(path.lastIndexOf('/') + 1);

  return base.replace(/\.java\.patch$/, '').replace(/\.java$/, '');
}

/**
 * 一行引用：chip 是「这是什么」（类/方法/补丁），后面跟位置与行号，点击打开右侧抽屉。
 *
 * 两处刻意不显示完整信息，否则长路径会把行撑宽、并弹出横向滚动条：
 *
 * 1. **类级引用只留行号**。类的 `symbol` 是全限定名（`net.minecraft.world.item…EnchantmentsByCostWithDifficulty`），
 *    它和后面的 `net/minecraft/world/item/…/EnchantmentsByCostWithDifficulty.java` 是同一件事的两种写法，
 *    两份都画出来既是重复、也必然溢出。chip 改成短名，正文只留 `:19-44`。
 * 2. **路径省略中间段**，只留文件名与行号（完整路径在 hover 提示里），并强制单行省略，
 *    保证任何长度的路径都不会撑破布局。
 */
function CitationRow({ citation, onOpen }: { citation: Citation; onOpen: () => void }) {
  const lines = citation.lines;
  const isPatch = citation.path.endsWith('.patch');
  const stem = fileStem(citation.path);
  const symbol = citation.symbol ?? '';

  // 类的 `symbol` 就是全限定名：基名相同 → 与文件名重复，正文不必再写一遍。
  const redundant = symbol !== '' && (symbol.split('.').pop() === stem || isPatch);

  const chipLabel = isPatch ? '补丁' : symbol !== '' ? shortSymbol(symbol) : '代码位置';

  return (
    <Tooltip title={`${citation.path}${symbol === '' ? '' : ` · ${symbol}`}`} placement="top-start">
      <Stack
        direction="row"
        spacing={1}
        sx={{
          alignItems: 'center',
          cursor: 'pointer',
          borderRadius: 1,
          px: 1,
          py: 0.25,
          minWidth: 0,
          '&:hover': { backgroundColor: 'action.hover' },
        }}
        onClick={onOpen}
      >
        <Chip
          size="small"
          variant="outlined"
          label={chipLabel}
          sx={{
            flexShrink: 0,
            maxWidth: 320,
            '& .MuiChip-label': { overflow: 'hidden', textOverflow: 'ellipsis' },
          }}
        />

        {/* `minWidth: 0` 是单行省略的前提：flex 子项默认不会收缩到内容宽度以下。 */}
        <Box
          component="span"
          sx={{
            fontFamily: 'monospace',
            fontSize: 13,
            flex: '1 1 auto',
            minWidth: 0,
            overflow: 'hidden',
            textOverflow: 'ellipsis',
            whiteSpace: 'nowrap',
            color: 'text.secondary',
          }}
        >
          {redundant ? `:${lines.join('-')}` : `${citation.path}:${lines.join('-')}`}
        </Box>

        <Typography variant="caption" sx={{ color: 'text.secondary', flexShrink: 0 }}>
          查看
        </Typography>
      </Stack>
    </Tooltip>
  );
}

/** `net.minecraft.world.entity.MobCategory` → `MobCategory`；`A.B#run` → `B#run`。 */
function shortSymbol(symbol: string): string {
  const [owner, method] = symbol.split('#');
  const short = owner.slice(owner.lastIndexOf('.') + 1);

  return method === undefined ? short : `${short}#${method}`;
}
