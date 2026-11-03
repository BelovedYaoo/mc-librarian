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
