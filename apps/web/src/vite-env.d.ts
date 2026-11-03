/// <reference types="vite/client" />

interface ImportMetaEnv {
  /** 后端根地址；留空时走同源 `/api`（开发期由 vite 代理，生产期由 Caddy 反代）。 */
  readonly VITE_SERVER_URL?: string;
}

interface ImportMeta {
  readonly env: ImportMetaEnv;
}
