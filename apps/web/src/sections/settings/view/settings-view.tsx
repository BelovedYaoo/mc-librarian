import type { AdminSettings, SettingsUpdate } from 'src/lib/api-client';

import Alert from '@mui/material/Alert';
import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Chip from '@mui/material/Chip';
import CircularProgress from '@mui/material/CircularProgress';
import Divider from '@mui/material/Divider';
import FormControlLabel from '@mui/material/FormControlLabel';
import Stack from '@mui/material/Stack';
import Switch from '@mui/material/Switch';
import TextField from '@mui/material/TextField';
import Typography from '@mui/material/Typography';
import { useEffect, useState } from 'react';

import { useAdminSettings } from 'src/hooks';

import { putSettings } from 'src/lib/api-client';

// ----------------------------------------------------------------------

/** 表单里的所有可改字段（密钥单独管，见 `Secrets`）。 */
type FormState = {
  llmBaseUrl: string;
  llmModel: string;
  llmTemperature: string;
  llmTimeoutS: string;
  askMaxSteps: string;
  askMaxAnswerTokens: string;
  askMaxContextTokens: string;
  askTimeoutS: string;
  askEnabled: boolean;
  indexEnabled: boolean;
  indexJavaXmx: string;
  indexJava: string;
  indexVineflowerJar: string;
  indexSpecialsourceJar: string;
  indexSummaryBatchSize: string;
  indexSummaryConcurrency: string;
  sessionTtlHours: string;
  inviteTtlHours: string;
  maxConcurrentAsks: string;
  dailyTokenThreshold: string;
};

type BlockName = 'llm' | 'ask' | 'index' | 'security' | 'alerts';

type FieldKind = 'text' | 'int' | 'float' | 'bool';

type FieldSpec = {
  name: keyof FormState;
  block: BlockName;
  /** 契约 `SettingsUpdate` 里的字段名。 */
  field: string;
  kind: FieldKind;
  label: string;
  helper?: string;
};

/**
 * 字段表：`name` 是表单状态，`block.field` 是契约路径。
 *
 * 保存时按这张表算出「改动过的字段」再提交——不整份回写，因为密钥永远拿不到原值。
 * `kind: 'bool'` 的字段渲染成开关（`index.enabled` / `ask.enabled`）。
 * `index.data_dir` 只读，不在这张表里。
 */
const FIELDS: FieldSpec[] = [
  {
    name: 'llmBaseUrl',
    block: 'llm',
    field: 'base_url',
    kind: 'text',
    label: '模型地址',
    helper: 'OpenAI 兼容端点，例如 https://api.deepseek.com/v1；留空 = 回退环境变量',
  },
  {
    name: 'llmModel',
    block: 'llm',
    field: 'model',
    kind: 'text',
    label: '模型名称',
    helper: '留空 = 回退环境变量',
  },
  {
    name: 'llmTemperature',
    block: 'llm',
    field: 'temperature',
    kind: 'float',
    label: '采样温度',
    helper: '0 = 确定性输出（摘要与问答都要求可复现）；留空 = 回退环境变量',
  },
  {
    name: 'llmTimeoutS',
    block: 'llm',
    field: 'timeout_s',
    kind: 'float',
    label: '单次请求超时（秒）',
    helper: '单次 LLM 请求的超时；留空 = 回退环境变量',
  },
  {
    name: 'askEnabled',
    block: 'ask',
    field: 'enabled',
    kind: 'bool',
    label: '启用问答（/v1/ask）',
  },
  {
    name: 'askMaxSteps',
    block: 'ask',
    field: 'max_steps',
    kind: 'int',
    label: '最大检索步数',
    helper: '问答请求省略 max_steps 时用它（1–12）',
  },
  {
    name: 'askMaxAnswerTokens',
    block: 'ask',
    field: 'max_answer_tokens',
    kind: 'int',
    label: '答案 token 上限',
    helper: '问答请求省略 max_answer_tokens 时用它（500–32000）',
  },
  {
    name: 'askMaxContextTokens',
    block: 'ask',
    field: 'max_context_tokens',
    kind: 'int',
    label: '上下文预算（token）',
    helper: '工具结果累积的上限，超出时从最旧的开始丢弃（≥ 1000）',
  },
  {
    name: 'askTimeoutS',
    block: 'ask',
    field: 'timeout_s',
    kind: 'int',
    label: '问答总超时（秒）',
    helper: '超时走降级回答，不返回 500',
  },
  {
    name: 'indexEnabled',
    block: 'index',
    field: 'enabled',
    kind: 'bool',
    label: '索引构建闸门（关掉后本实例不再构建索引）',
  },
  {
    name: 'indexJavaXmx',
    block: 'index',
    field: 'java_xmx',
    kind: 'text',
    label: '反编译内存（-Xmx）',
    helper: '形如 4G / 512M；只影响本实例跑的 download/decompile',
  },
  {
    name: 'indexJava',
    block: 'index',
    field: 'java',
    kind: 'text',
    label: 'java 可执行路径',
    helper: '留空 = 回退环境变量（默认 java）',
  },
  {
    name: 'indexVineflowerJar',
    block: 'index',
    field: 'vineflower_jar',
    kind: 'text',
    label: 'Vineflower jar 路径',
    helper: '反编译工具；留空 = 回退环境变量',
  },
  {
    name: 'indexSpecialsourceJar',
    block: 'index',
    field: 'specialsource_jar',
    kind: 'text',
    label: 'SpecialSource jar 路径',
    helper: 'remap 工具；留空 = 回退环境变量',
  },
  {
    name: 'indexSummaryBatchSize',
    block: 'index',
    field: 'summary_batch_size',
    kind: 'int',
    label: '摘要批次大小',
    helper: '每批多少个类（建议 30–50）',
  },
  {
    name: 'indexSummaryConcurrency',
    block: 'index',
    field: 'summary_concurrency',
    kind: 'int',
    label: '摘要并发批次',
  },
  {
    name: 'sessionTtlHours',
    block: 'security',
    field: 'session_ttl_hours',
    kind: 'int',
    label: '会话有效期（小时）',
    helper: '只影响之后新登录的会话',
  },
  {
    name: 'inviteTtlHours',
    block: 'security',
    field: 'invite_ttl_hours',
    kind: 'int',
    label: '邀请有效期（小时）',
    helper: '只影响之后创建的邀请链接',
  },
  {
    name: 'maxConcurrentAsks',
    block: 'security',
    field: 'max_concurrent_asks',
    kind: 'int',
    label: '并发问答上限',
    helper: '超出返回 429',
  },
  {
    name: 'dailyTokenThreshold',
    block: 'alerts',
    field: 'daily_token_threshold',
    kind: 'int',
    label: '每日 token 阈值',
    helper: '留空 = 不告警（不是 0）',
  },
];

/** 密钥字段：只写不读，表单里永远从空开始。 */
type SecretName = 'apiKey' | 'webhookUrl';

const SECRETS: {
  name: SecretName;
  block: BlockName;
  field: string;
  label: string;
  helper: string;
  isSet: (settings: AdminSettings) => boolean;
}[] = [
  {
    name: 'apiKey',
    block: 'llm',
    field: 'api_key',
    label: 'API 密钥',
    helper: '只写不读：保存后只显示「已设置」，明文不会回显',
    isSet: (settings) => settings.llm?.api_key_set === true,
  },
  {
    name: 'webhookUrl',
    block: 'alerts',
    field: 'webhook_url',
    label: '告警 Webhook',
    helper: '用量超阈值时推送；URL 里通常带 token，因此同样按密钥处理',
    isSet: (settings) => settings.alerts?.webhook_url_set === true,
  },
];

const GROUPS: { block: BlockName; title: string; hint?: string }[] = [
  { block: 'llm', title: '模型', hint: '摘要生成与问答共用同一个 OpenAI 兼容端点。' },
  { block: 'ask', title: '问答', hint: '「启用」关掉后 /v1/ask 一律 429，检索不受影响。' },
  { block: 'index', title: '索引', hint: '索引参数在下一个任务开始时生效；留空 = 回退环境变量。' },
  { block: 'security', title: '会话与并发' },
  { block: 'alerts', title: '用量告警' },
];

function toForm(settings: AdminSettings): FormState {
  const text = (value: string | number | null | undefined) =>
    value === null || value === undefined ? '' : String(value);

  return {
    llmBaseUrl: text(settings.llm?.base_url),
    llmModel: text(settings.llm?.model),
    llmTemperature: text(settings.llm?.temperature),
    llmTimeoutS: text(settings.llm?.timeout_s),
    askMaxSteps: text(settings.ask?.max_steps),
    askMaxAnswerTokens: text(settings.ask?.max_answer_tokens),
    askMaxContextTokens: text(settings.ask?.max_context_tokens),
    askTimeoutS: text(settings.ask?.timeout_s),
    askEnabled: settings.ask?.enabled === true,
    indexEnabled: settings.index?.enabled === true,
    indexJavaXmx: text(settings.index?.java_xmx),
    indexJava: text(settings.index?.java),
    indexVineflowerJar: text(settings.index?.vineflower_jar),
    indexSpecialsourceJar: text(settings.index?.specialsource_jar),
    indexSummaryBatchSize: text(settings.index?.summary_batch_size),
    indexSummaryConcurrency: text(settings.index?.summary_concurrency),
    sessionTtlHours: text(settings.security?.session_ttl_hours),
    inviteTtlHours: text(settings.security?.invite_ttl_hours),
    maxConcurrentAsks: text(settings.security?.max_concurrent_asks),
    dailyTokenThreshold: text(settings.alerts?.daily_token_threshold),
  };
}

/** 空字符串 = 清除该覆盖（回退环境变量），因此发 `null` 而不是空串。 */
function toValue(spec: FieldSpec, raw: string | boolean): string | number | boolean | null {
  if (spec.kind === 'bool') return raw === true;

  const value = String(raw).trim();

  if (value === '') return null;

  return spec.kind === 'text' ? value : Number(value);
}

/**
 * 只把**改动过**的字段装进请求体：部分更新是契约语义，也让「密钥不回显」天然成立
 * （没改就不提交，不会用空串把已设置的密钥抹掉）。
 */
function buildUpdate(
  form: FormState,
  snapshot: FormState,
  secretInput: Record<SecretName, string>,
  cleared: SecretName[]
): SettingsUpdate {
  const body: Record<string, Record<string, unknown>> = {};

  const put = (block: BlockName, field: string, value: unknown) => {
    body[block] = { ...body[block], [field]: value };
  };

  FIELDS.forEach((spec) => {
    if (form[spec.name] === snapshot[spec.name]) return;

    put(spec.block, spec.field, toValue(spec, form[spec.name]));
  });

  SECRETS.forEach((secret) => {
    const typed = secretInput[secret.name].trim();

    if (typed !== '') put(secret.block, secret.field, typed);
    else if (cleared.includes(secret.name)) put(secret.block, secret.field, '');
  });

  return body as SettingsUpdate;
}

const EMPTY_SECRETS: Record<SecretName, string> = { apiKey: '', webhookUrl: '' };

// ----------------------------------------------------------------------

/**
 * 「实例设置」区块（仅 admin）：模型 / 问答 / 索引 / 会话 / 告警五组表单。
 *
 * 保存时只提交改动过的字段（部分更新），成功后刷新当前值并给出成功反馈。
 * 密钥只写不读：输入框永远从空开始，已设置时显示标签 +「清除」按钮。
 */
export function SettingsView() {
  const { state, refresh } = useAdminSettings();
  const [form, setForm] = useState<FormState | null>(null);
  const [snapshot, setSnapshot] = useState<FormState | null>(null);
  const [loaded, setLoaded] = useState<AdminSettings | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [secretInput, setSecretInput] = useState<Record<SecretName, string>>(EMPTY_SECRETS);
  const [cleared, setCleared] = useState<SecretName[]>([]);
  const fetched = state.status === 'ready' ? state.settings : null;

  // 保存后 `refresh()` 会再拉一次：以服务端返回的值重建表单（而不是拿本地状态猜），
  // 未拿到（加载中/失败）时保留上一次的值，避免表单闪一下空白。
  useEffect(() => {
    if (!fetched) return;

    const next = toForm(fetched);

    setLoaded(fetched);
    setForm(next);
    setSnapshot(next);
    setSecretInput(EMPTY_SECRETS);
    setCleared([]);
  }, [fetched]);

  if (state.status === 'error') {
    return <Alert severity="error">{state.message}</Alert>;
  }

  if (!form || !snapshot || !loaded) {
    if (state.status === 'loading') {
      return (
        <Stack sx={{ py: 4, alignItems: 'center' }}>
          <CircularProgress size={22} />
        </Stack>
      );
    }

    return (
      <Alert severity="warning">
        读不到实例设置：需要管理员账号（非管理员打开本页只显示自己的访问令牌）。
      </Alert>
    );
  }

  const settings = loaded;

  const dirty =
    cleared.length > 0 ||
    FIELDS.some((spec) => form[spec.name] !== snapshot[spec.name]) ||
    (Object.keys(secretInput) as SecretName[]).some((name) => secretInput[name].trim() !== '');

  const set = (name: keyof FormState, value: string | boolean) => {
    setForm((current) => (current ? { ...current, [name]: value } : current));
    setSaved(false);
  };

  const handleSave = async () => {
    setSubmitting(true);
    setError(null);
    setSaved(false);

    try {
      await putSettings(buildUpdate(form, snapshot, secretInput, cleared));
      setSaved(true);
      refresh();
    } catch (caught) {
      setError(caught instanceof Error ? caught.message : String(caught));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <Stack spacing={3}>
      <Stack direction="row" spacing={2} sx={{ alignItems: 'center' }}>
        <Typography variant="h6">实例设置</Typography>
        <Box sx={{ flex: '1 1 auto' }} />

        <Button size="small" variant="outlined" onClick={refresh} disabled={submitting}>
          重新读取
        </Button>
        <Button
          size="small"
          variant="contained"
          onClick={() => void handleSave()}
          disabled={submitting || !dirty}
        >
          {submitting ? '保存中…' : '保存'}
        </Button>
      </Stack>

      {error && <Alert severity="error">{error}</Alert>}
      {saved && <Alert severity="success">已保存并立即生效。</Alert>}

      <Typography variant="body2" sx={{ color: 'text.secondary' }}>
        只提交改动过的字段；留空表示回退到部署时的环境变量（不是改成 0）。
      </Typography>

      {GROUPS.map((group) => (
        <Card key={group.block}>
          <CardContent>
            <Typography variant="subtitle1">{group.title}</Typography>
            {group.hint && (
              <Typography variant="body2" sx={{ color: 'text.secondary', mb: 1 }}>
                {group.hint}
              </Typography>
            )}

            <Divider sx={{ my: 2 }} />

            <Stack spacing={2}>
              {FIELDS.filter((spec) => spec.block === group.block).map((spec) =>
                spec.kind === 'bool' ? (
                  <FormControlLabel
                    key={spec.name}
                    control={
                      <Switch
                        checked={form[spec.name] === true}
                        onChange={(event) => set(spec.name, event.target.checked)}
                      />
                    }
                    label={spec.label}
                  />
                ) : (
                  <TextField
                    key={spec.name}
                    label={spec.label}
                    helperText={spec.helper}
                    value={String(form[spec.name])}
                    onChange={(event) => set(spec.name, event.target.value)}
                    type={spec.kind === 'text' ? 'text' : 'number'}
                    size="small"
                    fullWidth
                  />
                )
              )}

              {SECRETS.filter((secret) => secret.block === group.block).map((secret) => (
                <SecretField
                  key={secret.name}
                  label={secret.label}
                  helper={secret.helper}
                  isSet={secret.isSet(settings)}
                  cleared={cleared.includes(secret.name)}
                  value={secretInput[secret.name]}
                  onChange={(value) => {
                    setSecretInput((current) => ({ ...current, [secret.name]: value }));
                    setSaved(false);
                  }}
                  onClear={() => {
                    setCleared((current) => [...current, secret.name]);
                    setSecretInput((current) => ({ ...current, [secret.name]: '' }));
                    setSaved(false);
                  }}
                  onReset={() => {
                    setCleared((current) => current.filter((name) => name !== secret.name));
                    setSaved(false);
                  }}
                />
              ))}
            </Stack>
          </CardContent>
        </Card>
      ))}
    </Stack>
  );
}

// ----------------------------------------------------------------------

type SecretFieldProps = {
  label: string;
  helper: string;
  isSet: boolean;
  cleared: boolean;
  value: string;
  onChange: (value: string) => void;
  onClear: () => void;
  onReset: () => void;
};

/** 密钥输入：显示「已设置/未设置」+「重新设置 / 清除」，永远不回显已存的值。 */
function SecretField({
  label,
  helper,
  isSet,
  cleared,
  value,
  onChange,
  onClear,
  onReset,
}: SecretFieldProps) {
  return (
    <Stack spacing={1}>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
        <Typography variant="body2">{label}</Typography>
        <Chip
          size="small"
          color={isSet && !cleared ? 'success' : 'default'}
          label={isSet && !cleared ? '已设置' : '未设置'}
        />
        <Box sx={{ flex: '1 1 auto' }} />
        {isSet && !cleared && (
          <Button size="small" color="error" onClick={onClear}>
            清除
          </Button>
        )}
        {cleared && (
          <Button size="small" onClick={onReset}>
            取消清除
          </Button>
        )}
      </Stack>

      <TextField
        label={`${label}（留空 = 不改）`}
        helperText={cleared ? '保存后该密钥被清除（回退环境变量）' : helper}
        value={value}
        onChange={(event) => onChange(event.target.value)}
        type="password"
        size="small"
        autoComplete="off"
        fullWidth
      />
    </Stack>
  );
}
