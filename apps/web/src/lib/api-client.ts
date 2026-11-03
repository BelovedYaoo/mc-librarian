import type { components, paths } from 'src/types/api';

import { CONFIG } from 'src/global-config';

// ----------------------------------------------------------------------

/**
 * 请求 / 响应类型一律从契约生成（`bun run --cwd apps/web gen:api`），不手写。
 */
export type HealthResponse =
  paths['/v1/health']['get']['responses'][200]['content']['application/json'];

/** 当前用户 = 登录 / setup 的响应体，也是 `/v1/auth/me` 的响应体。 */
export type User = paths['/v1/auth/me']['get']['responses'][200]['content']['application/json'];

/** `GET /v1/setup` 的响应体：控制面是否还需要初始化。 */
export type SetupStatus =
  paths['/v1/setup']['get']['responses'][200]['content']['application/json'];

export type LoginRequest =
  paths['/v1/auth/login']['post']['requestBody']['content']['application/json'];

export type SetupRequest = paths['/v1/setup']['post']['requestBody']['content']['application/json'];

/** 个人访问令牌（列表项 / 创建响应的 `info`）：**不含**明文。 */
export type TokenInfo = components['schemas']['TokenInfo'];

export type TokenListResponse =
  paths['/v1/auth/tokens']['get']['responses'][200]['content']['application/json'];

export type TokenCreateRequest =
  paths['/v1/auth/tokens']['post']['requestBody']['content']['application/json'];

export type TokenCreated =
  paths['/v1/auth/tokens']['post']['responses'][201]['content']['application/json'];
/** `GET /v1/admin/users` 的响应体。 */
export type UserList =
  paths['/v1/admin/users']['get']['responses'][200]['content']['application/json'];

/** `POST /v1/admin/users` 的响应体：用户 + **仅此一次**返回的邀请链接。 */
export type CreatedUser =
  paths['/v1/admin/users']['post']['responses'][201]['content']['application/json'];

export type CreateUserRequest =
  paths['/v1/admin/users']['post']['requestBody']['content']['application/json'];

export type PatchUserRequest =
  paths['/v1/admin/users/{userId}']['patch']['requestBody']['content']['application/json'];

/** `GET /v1/invites/{token}` 的响应体：链接是否仍可用。 */
export type InviteStatus =
  paths['/v1/invites/{token}']['get']['responses'][200]['content']['application/json'];

export type AcceptInviteRequest =
  paths['/v1/invites/{token}/accept']['post']['requestBody']['content']['application/json'];

/** 版本注册表里的一行（`GET /v1/versions`）。 */
export type VersionInfo = components['schemas']['VersionInfo'];

/** 实例设置（`GET /v1/admin/settings`）。当前只读，未实现的块整块省略。 */
export type AdminSettings = components['schemas']['Settings'];

/** 审计流水里的一行：谁在什么时候对什么做了什么（`GET /v1/admin/audit`）。 */
export type AuditEntry = components['schemas']['AuditEntry'];

/** 索引任务；`stage` / `progress` 由 JobRunner 推进。 */
export type Job = components['schemas']['Job'];

export type JobStatus = Job['status'];

export type JobStage = NonNullable<Job['stage']>;

export type JobCreate = components['schemas']['JobCreate'];

export type JobLog = components['schemas']['JobLog'];

/** 符号查找的匹配级别开关（`any` 不过滤）。 */
export type SymbolKind = components['schemas']['SymbolKind'];

/** 一条符号命中（`GET /v1/symbol`）。 */
export type SymbolDetail = components['schemas']['SymbolDetail'];

export type SymbolMatches =
  paths['/v1/symbol']['get']['responses'][200]['content']['application/json'];

/** 混合检索请求体（`POST /v1/search`）。 */
export type SearchRequest =
  paths['/v1/search']['post']['requestBody']['content']['application/json'];

/** 混合检索响应体（`POST /v1/search`）：融合后的命中 + 建议。 */
export type SearchResponse =
  paths['/v1/search']['post']['responses'][200]['content']['application/json'];

/** 一条融合命中；`why` 是来源通道数组。 */
export type Hit = components['schemas']['Hit'];

/** 命中来源通道（`summary` 由 #7 落地后才会出现）。 */
export type HitChannel = Hit['why'][number];

/** 反查引用的响应体（`GET /v1/references`）。 */
export type ReferenceResponse =
  paths['/v1/references']['get']['responses'][200]['content']['application/json'];

/** 文件骨架（`GET /v1/outline`）；不含方法体。 */
export type Outline = paths['/v1/outline']['get']['responses'][200]['content']['application/json'];

export type OutlineClass = Outline['classes'][number];

export type OutlineMethod = NonNullable<OutlineClass['methods']>[number];

/** 源码切片（`GET /v1/read`）。 */
export type FileSlice = paths['/v1/read']['get']['responses'][200]['content']['application/json'];

/** RFC 7807 错误体，直接取契约的 `Problem`，不从某一条响应里反推。 */
export type Problem = components['schemas']['Problem'];

export class ApiError extends Error {
  readonly status: number;

  readonly title: string | null;

  constructor(status: number, message: string, title: string | null = null) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.title = title;
  }
}

/** 后端错误体是 RFC 7807 `application/problem+json`，优先拿 `detail` 当文案。 */
async function toApiError(response: Response): Promise<ApiError> {
  try {
    const problem = (await response.json()) as Problem;

    return new ApiError(
      response.status,
      problem.detail ?? problem.title ?? `${response.status} ${response.statusText}`,
      problem.title ?? null
    );
  } catch {
    return new ApiError(response.status, `${response.status} ${response.statusText}`);
  }
}

// ----------------------------------------------------------------------
// 统一的 401 处理：会话失效时由 AuthProvider 注册回调，清空会话并跳登录页。
// 用注册回调而不是在这里 import auth，是为了避免 api-client ↔ auth 的循环依赖。
// ----------------------------------------------------------------------

export type UnauthorizedHandler = () => void;

let unauthorizedHandler: UnauthorizedHandler | null = null;

export function setUnauthorizedHandler(handler: UnauthorizedHandler | null): void {
  unauthorizedHandler = handler;
}

function notifyUnauthorized(): void {
  unauthorizedHandler?.();
}

type RequestOptions = RequestInit & {
  /** 登录 / setup 自身返回的 401 是业务结果，不该触发「会话失效」跳转。 */
  skipUnauthorizedHandler?: boolean;
};

async function request<T>(
  path: string,
  { skipUnauthorizedHandler = false, ...init }: RequestOptions = {}
): Promise<T> {
  const response = await fetch(`${CONFIG.serverUrl}${path}`, {
    // 会话靠 cookie。用 include 而不是 same-origin：`VITE_SERVER_URL` 指绝对地址
    // （跨源部署 / 独立 API 域名）时，same-origin 不会带上 cookie。
    credentials: 'include',
    ...init,
    headers: { Accept: 'application/json', ...init.headers },
  });

  if (!response.ok) {
    const error = await toApiError(response);

    if (response.status === 401 && !skipUnauthorizedHandler) {
      notifyUnauthorized();
    }

    throw error;
  }

  // 204 没有响应体（登出）。
  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

function postJson<T>(path: string, body: unknown, options: RequestOptions = {}): Promise<T> {
  return request<T>(path, {
    ...options,
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

function patchJson<T>(path: string, body: unknown, options: RequestOptions = {}): Promise<T> {
  return request<T>(path, {
    ...options,
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/**
 * 存活探针。开发期 `CONFIG.serverUrl` 是 `/api`，由 vite 代理到后端；
 * 生产期由反向代理转发，前端代码不需要区分环境。
 */
export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return request<HealthResponse>('/v1/health', { signal });
}

/** 首次启动引导：`required` 为 true 时前端必须先把用户送到 `/setup`。 */
export function getSetupStatus(signal?: AbortSignal): Promise<SetupStatus> {
  return request<SetupStatus>('/v1/setup', { signal });
}

/** 创建唯一管理员；成功后后端直接下发会话 cookie，因此返回即已登录。 */
export function createFirstAdmin(body: SetupRequest): Promise<User> {
  return postJson<User>('/v1/setup', body, { skipUnauthorizedHandler: true });
}

export function login(body: LoginRequest): Promise<User> {
  return postJson<User>('/v1/auth/login', body, { skipUnauthorizedHandler: true });
}

export function logout(): Promise<void> {
  return request<void>('/v1/auth/logout', { method: 'POST' });
}

export function getMe(signal?: AbortSignal): Promise<User> {
  return request<User>('/v1/auth/me', { signal });
}

/** 我的访问令牌列表；只返回前缀，不含明文。 */
export function listTokens(signal?: AbortSignal): Promise<TokenListResponse> {
  return request<TokenListResponse>('/v1/auth/tokens', { signal });
}

/** 签发访问令牌；响应里的明文 `token` 只返回这一次。 */
export function createToken(body: TokenCreateRequest): Promise<TokenCreated> {
  return postJson<TokenCreated>('/v1/auth/tokens', body);
}

/** 吊销访问令牌；吊销后下一次用该令牌请求立即 401。 */
export function revokeToken(tokenId: string): Promise<void> {
  return request<void>(`/v1/auth/tokens/${tokenId}`, { method: 'DELETE' });
}

/** 用户列表（仅 admin；非 admin 由后端返回 403）。 */
export function listUsers(signal?: AbortSignal): Promise<UserList> {
  return request<UserList>('/v1/admin/users', { signal });
}

/** 建号：管理员不设密码，后端返回仅此一次可见的邀请链接。 */
export function createUser(body: CreateUserRequest): Promise<CreatedUser> {
  return postJson<CreatedUser>('/v1/admin/users', body);
}

/** 改角色 / 启停（仅 admin）；违反最后管理员保护时后端返回 409。 */
export function patchUser(userId: string, body: PatchUserRequest): Promise<User> {
  return patchJson<User>(`/v1/admin/users/${encodeURIComponent(userId)}`, body);
}

/** 校验邀请链接是否仍可用（公开）。 */
export function getInvite(token: string, signal?: AbortSignal): Promise<InviteStatus> {
  return request<InviteStatus>(`/v1/invites/${encodeURIComponent(token)}`, { signal });
}

/** 接受邀请：设密码 + 激活 + 直接登录（后端下发会话 cookie）。 */
export function acceptInvite(token: string, body: AcceptInviteRequest): Promise<User> {
  return postJson<User>(`/v1/invites/${encodeURIComponent(token)}/accept`, body, {
    skipUnauthorizedHandler: true,
  });
}

// ----------------------------------------------------------------------
// 版本与索引任务（admin）
// ----------------------------------------------------------------------

/** 拼 `?a=1&b=2`，忽略 `undefined` 与空串；`false` 要发出去（如 `fuzzy=false`）。 */
function buildQuery(params: Record<string, string | number | boolean | undefined>): string {
  const searchParams = new URLSearchParams();

  Object.entries(params).forEach(([key, value]) => {
    if (value !== undefined && value !== '') searchParams.set(key, String(value));
  });

  const query = searchParams.toString();

  return query ? `?${query}` : '';
}

/** 已索引版本列表（登录后可读）。 */
export async function listVersions(signal?: AbortSignal): Promise<VersionInfo[]> {
  const body = await request<{ versions?: VersionInfo[] }>('/v1/versions', { signal });

  return body.versions ?? [];
}

/** 实例设置（仅 admin）。只读、值从运行时配置派生；未实现的块整块省略。 */
export function getSettings(signal?: AbortSignal): Promise<AdminSettings> {
  return request<AdminSettings>('/v1/admin/settings', { signal });
}

/** 注册已上传的 `db/<version>.sqlite`（管理员）。 */
export function registerVersion(body: {
  version: string;
  obfuscated?: boolean;
}): Promise<VersionInfo> {
  return postJson<VersionInfo>('/v1/admin/versions', body);
}

/** 任务列表；`status` 不传则不过滤。 */
export async function listJobs(
  params: { status?: JobStatus; limit?: number } = {},
  signal?: AbortSignal
): Promise<Job[]> {
  const body = await request<{ jobs?: Job[] }>(`/v1/admin/jobs${buildQuery(params)}`, { signal });

  return body.jobs ?? [];
}

/** 入队一个索引任务，后端立即返回 `202`。 */
export function createJob(body: JobCreate): Promise<Job> {
  return postJson<Job>('/v1/admin/jobs', body);
}

export function getJob(jobId: string, signal?: AbortSignal): Promise<Job> {
  return request<Job>(`/v1/admin/jobs/${jobId}`, { signal });
}

/** 按 `after` 增量拉取日志，供日志面板轮询。 */
export async function getJobLogs(
  jobId: string,
  params: { after?: number; limit?: number } = {},
  signal?: AbortSignal
): Promise<JobLog[]> {
  const body = await request<{ lines?: JobLog[] }>(
    `/v1/admin/jobs/${jobId}/logs${buildQuery(params)}`,
    { signal }
  );

  return body.lines ?? [];
}

/** 取消任务；queued 立即转 canceled，running 在当前阶段边界停止。 */
export function cancelJob(jobId: string): Promise<Job> {
  return request<Job>(`/v1/admin/jobs/${jobId}/cancel`, { method: 'POST' });
}

/** 重试失败/已取消的任务：后端保留 checkpoint，从下一个未完成阶段续跑。 */
export function retryJob(jobId: string): Promise<Job> {
  return request<Job>(`/v1/admin/jobs/${jobId}/retry`, { method: 'POST' });
}

/** 审计流水（仅 admin）；`actor` 给定时按用户名精确筛选。 */
export async function listAudit(
  params: { actor?: string; limit?: number } = {},
  signal?: AbortSignal
): Promise<AuditEntry[]> {
  const body = await request<{ entries?: AuditEntry[] }>(
    `/v1/admin/audit${buildQuery(params)}`,
    { signal }
  );

  return body.entries ?? [];
}

// ----------------------------------------------------------------------
// 检索与阅读（登录即可用）
// ----------------------------------------------------------------------

/** 符号查找；`fuzzy=false` 只做全限定名与简单名精确匹配。 */
export function getSymbol(
  params: { version: string; name: string; fuzzy?: boolean; kind?: SymbolKind },
  signal?: AbortSignal
): Promise<SymbolMatches> {
  return request<SymbolMatches>(`/v1/symbol${buildQuery(params)}`, { signal });
}

/** 混合检索：三路召回（symbol / literal / reference）在服务端用 RRF 融合。 */
export function search(body: SearchRequest, signal?: AbortSignal): Promise<SearchResponse> {
  return postJson<SearchResponse>('/v1/search', body, { signal });
}

/** 反查引用；`symbol` 是方法名或 `Class#method`。 */
export function getReferences(
  params: { version: string; symbol: string; limit?: number },
  signal?: AbortSignal
): Promise<ReferenceResponse> {
  return request<ReferenceResponse>(`/v1/references${buildQuery(params)}`, { signal });
}

/** 文件骨架：包名、imports、类与方法签名和行号。 */
export function getOutline(
  params: { version: string; path: string },
  signal?: AbortSignal
): Promise<Outline> {
  return request<Outline>(`/v1/outline${buildQuery(params)}`, { signal });
}

/** 源码切片：`start`/`end` 必填；超出 `max_tokens` 时服务端按行从尾部截断。 */
export function readFile(
  params: { version: string; path: string; start: number; end: number; max_tokens?: number },
  signal?: AbortSignal
): Promise<FileSlice> {
  return request<FileSlice>(`/v1/read${buildQuery(params)}`, { signal });
}
