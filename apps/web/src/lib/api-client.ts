import type { paths } from 'src/types/api';

import { CONFIG } from 'src/global-config';

// ----------------------------------------------------------------------

/** 契约里 `GET /v1/health` 的 200 响应体；由 openapi-typescript 生成，不手写。 */
export type HealthResponse =
  paths['/v1/health']['get']['responses'][200]['content']['application/json'];

export class ApiError extends Error {
  readonly status: number;

  constructor(status: number, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const response = await fetch(`${CONFIG.serverUrl}${path}`, {
    ...init,
    headers: { Accept: 'application/json', ...init.headers },
  });

  if (!response.ok) {
    throw new ApiError(response.status, `${response.status} ${response.statusText}`);
  }

  return (await response.json()) as T;
}

/**
 * 存活探针。开发期 `CONFIG.serverUrl` 是 `/api`，由 vite 代理到后端；
 * 生产期由 Caddy 反代，前端代码不需要区分环境。
 */
export function getHealth(signal?: AbortSignal): Promise<HealthResponse> {
  return request<HealthResponse>('/v1/health', { signal });
}
