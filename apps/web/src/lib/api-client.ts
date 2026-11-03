import type { paths } from 'src/types/api';

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

type Problem =
  paths['/v1/auth/login']['post']['responses'][401]['content']['application/problem+json'];

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

/** 后端错误体是 RFC 7807 `application/problem+json`，优先拿 `detail` 当文案。 */
async function toApiError(response: Response): Promise<ApiError> {
  try {
    const problem = (await response.json()) as Problem;

    return new ApiError(
      response.status,
      problem.detail ?? problem.title ?? `${response.status} ${response.statusText}`
    );
  } catch {
    return new ApiError(response.status, `${response.status} ${response.statusText}`);
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${CONFIG.serverUrl}${path}`, {
    // 会话靠 cookie，同源请求必须带上凭据。
    credentials: 'same-origin',
    ...init,
    headers: { Accept: 'application/json', ...init.headers },
  });

  if (!response.ok) {
    throw await toApiError(response);
  }

  // 204 没有响应体（登出）。
  if (response.status === 204) {
    return undefined as T;
  }

  return (await response.json()) as T;
}

function postJson<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, {
    method: 'POST',
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
  return postJson<User>('/v1/setup', body);
}

export function login(body: LoginRequest): Promise<User> {
  return postJson<User>('/v1/auth/login', body);
}

export function logout(): Promise<void> {
  return request<void>('/v1/auth/logout', { method: 'POST' });
}

export function getMe(signal?: AbortSignal): Promise<User> {
  return request<User>('/v1/auth/me', { signal });
}
