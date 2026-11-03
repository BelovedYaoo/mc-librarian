import packageJson from '../package.json';

// ----------------------------------------------------------------------

export type ConfigValue = {
  appName: string;
  appVersion: string;
  /**
   * 后端根地址。默认 `/api`：开发期由 vite 代理到 `127.0.0.1:8787`，
   * 生产期由 Caddy 反代到 api 容器，前端不需要知道真实地址。
   */
  serverUrl: string;
};

// ----------------------------------------------------------------------

export const CONFIG: ConfigValue = {
  appName: 'MC Librarian',
  appVersion: packageJson.version,
  serverUrl: import.meta.env.VITE_SERVER_URL || '/api',
};
