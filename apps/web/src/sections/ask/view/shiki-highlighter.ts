import type { HighlighterCore, ThemedToken } from 'shiki/core';

// ----------------------------------------------------------------------

/** 抽屉会渲染的两种语言；其它后缀回退纯文本。 */
export type HighlightLanguage = 'java' | 'json';

/** 主题跟随 MUI 明暗（`useTheme().palette.mode`）。 */
export type HighlightTheme = 'github-light' | 'github-dark';

/** 一行的高亮结果：Shiki 的 token 序列（每段带 `color`）。 */
export type HighlightedLine = ThemedToken[];

/**
 * 全局单例：高亮器只建一次，两种语言与两套主题都只加载一次。
 *
 * 走细粒度入口（`shiki/core` + 显式 `import()`）而不是 `shiki` 的 `createHighlighter`：
 * 后者的源码注释写明「Importing this function will bundle all languages and themes」，
 * 而抽屉只需要 `java` / `json`，没必要把全部语法打进包里。各子模块用动态 `import()`
 * 声明，构建时自然切成独立 chunk，只在第一次打开抽屉时才下载。
 */
let highlighter: Promise<HighlighterCore> | null = null;

function loadHighlighter(): Promise<HighlighterCore> {
  highlighter ??= Promise.all([
    import('shiki/core'),
    import('shiki/engine/javascript'),
    import('shiki/langs/java.mjs'),
    import('shiki/langs/json.mjs'),
    import('shiki/themes/github-light.mjs'),
    import('shiki/themes/github-dark.mjs'),
  ]).then(([core, engine, java, json, light, dark]) =>
    core.createHighlighterCore({
      themes: [light.default, dark.default],
      langs: [java.default, json.default],
      engine: engine.createJavaScriptRegexEngine(),
    })
  );

  return highlighter;
}

/** 路径后缀 → 语言；`.java` / `.json` 之外一律纯文本（`text` 不需要注册）。 */
export function languageForPath(path: string): HighlightLanguage | 'text' {
  if (path.endsWith('.java')) return 'java';

  if (path.endsWith('.json')) return 'json';

  return 'text';
}

/** 明暗模式 → Shiki 主题名。 */
export function themeForMode(mode: 'light' | 'dark'): HighlightTheme {
  return mode === 'dark' ? 'github-dark' : 'github-light';
}

/** 把一段源码切成逐行 token；只传可视窗口的代码，不传整文件。 */
export async function highlightLines(
  code: string,
  lang: HighlightLanguage,
  theme: HighlightTheme
): Promise<HighlightedLine[]> {
  const instance = await loadHighlighter();

  return instance.codeToTokens(code, { lang, theme }).tokens;
}
