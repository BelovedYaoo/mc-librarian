/**
 * 本地工具链的主密钥兜底（issue #11 的启动护栏不该打断 `bun run dev` / `bun run smoke`）。
 *
 * 服务端在 `MC_LIBRARIAN_SECRET` 缺失或短于 16 字符时**启动即失败**：`settings` 表里的密钥
 * （`llm.api_key` / `alerts.webhook_url`）用 AES-GCM 加密，自动生成并落盘会让重启后旧密文
 * 解不开（见 `apps/api/src/mc_librarian/secretbox.py`）。这条护栏对生产是对的，但本地开发与
 * 冒烟测试并不需要真实的密钥材料，缺它就等于把工具链打断了。
 *
 * 所以这里给一个**固定**的开发用值（固定才不会让上次写入的密文在下次启动时解不开），并把
 * 「用了兜底值」明确打出来——不静默注入。**生产必须显式设置**：这个值写在仓库里，
 * 谁都能解开用它加密的密文。
 */
export const SECRET_ENV = 'MC_LIBRARIAN_SECRET';

/** 32 个字符的固定开发用值；只够过 `MIN_SECRET_LENGTH`，不是密钥材料。 */
export const DEV_SECRET = 'mc-librarian-dev-secret-01234567';

/** 环境里没有 `MC_LIBRARIAN_SECRET` 时补上固定的开发用值并打一条日志；已有则原样返回。 */
export function withDevSecret(env) {
  if ((env[SECRET_ENV] ?? '').trim()) return env;

  console.log(
    `\u001b[33m未设置 ${SECRET_ENV}，本次使用开发默认值（仅限本地开发与冒烟测试；生产必须显式设置）\u001b[0m`
  );

  return { ...env, [SECRET_ENV]: DEV_SECRET };
}
