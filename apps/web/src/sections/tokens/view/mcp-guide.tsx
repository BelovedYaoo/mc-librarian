import Box from '@mui/material/Box';
import Button from '@mui/material/Button';
import Card from '@mui/material/Card';
import CardContent from '@mui/material/CardContent';
import Divider from '@mui/material/Divider';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';
import { useState } from 'react';

// ----------------------------------------------------------------------

/** MCP 暴露的工具（与 `apps/api/src/mc_librarian/mcp/server.py` 一一对应）。 */
const TOOLS: { name: string; what: string }[] = [
  { name: 'versions', what: '列出可检索的版本（其它工具的 version 必须取自这里）' },
  { name: 'search', what: '多路融合检索：中文摘要 / 符号 / 字面量 / 引用 / 补丁' },
  { name: 'symbol', what: '按名字找类、方法、字段（含签名与行号）' },
  { name: 'outline', what: '文件骨架：类、方法、字段及其行区间' },
  { name: 'read', what: '按行区间读源码或补丁正文' },
  { name: 'references', what: '反查某个符号在哪儿被调用/引用' },
  { name: 'ask', what: '服务端跑完整检索循环，直接给带引用的中文答案（较慢）' },
];

/** 接入说明的 Markdown 版本：一键复制后可以直接粘给 AI，让它自己配。 */
function buildMarkdown(origin: string): string {
  return `# mc-librarian MCP 接入

服务地址（streamable HTTP，需 Bearer PAT 鉴权）：

    ${origin}/mcp

## 1. 拿一个访问令牌

在本页「访问令牌」里点「新建令牌」，明文只显示一次，格式 \`mcl_…\`。
权限与当前账号完全一致，过期后需重新签发。

## 2. 客户端配置

通用写法（pi / Claude Code / Cursor 都认这个结构）：

\`\`\`json
{
  "mcpServers": {
    "mc-librarian": {
      "type": "http",
      "url": "${origin}/mcp",
      "headers": { "Authorization": "Bearer <你的 mcl_ 令牌>" },
      "exposure": "direct",
      "timeout": 300
    }
  }
}
\`\`\`

- \`exposure: "direct"\`：pi 的默认值是 \`codemode\`，那会把工具**注册但不声明给模型**，
  表现为「连上了但模型看不到工具」。用 direct 才能直接调用。
- \`timeout: 300\`：\`ask\` 一次可能跑 30–90 秒，默认 60 秒会先超时。

## 3. 确认连通

\`\`\`bash
curl -s ${origin}/mcp \\
  -H "Authorization: Bearer <你的 mcl_ 令牌>" \\
  -H "Content-Type: application/json" \\
  -H "Accept: application/json, text/event-stream" \\
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}'
\`\`\`

返回 \`serverInfo\` 即通；返回 401 是令牌无效或已过期。

## 4. 可用工具

${TOOLS.map((tool) => `- \`${tool.name}\`：${tool.what}`).join('\n')}

## 注意

- 先调 \`versions\`：版本串（如 \`forge-47.4.16+1.20.1\`）是模型猜不出来的。
- \`search\` 的 \`source\` 可过滤 \`patched\`（改过的 MC 源码）/ \`loader\`（加载器自身）/ \`patch\`（补丁片段）。
- 每个版本有两份独立产物：索引库（检索用）与源码树（\`read\`/\`outline\` 用）；
  \`versions\` 的 \`searchable\` 与 \`sources_available\` 分别表示这两件事。
`;
}

// ----------------------------------------------------------------------

/**
 * MCP 接入说明（`/mcp`）。
 *
 * 之前只有一句「供脚本用 Bearer 调用」，用户既不知道 MCP 端点的存在，也不知道怎么配到
 * 自己的 AI 客户端里。这里把「端点 + 令牌 + 客户端 JSON + 连通性验证 + 工具清单」放在一处，
 * 并给一个**复制成 Markdown** 的按钮——可以直接发给 AI 让它自己接。
 *
 * 地址由 `window.location.origin` 生成，不为每个部署各写一份。
 */
export function McpGuide() {
  const [copied, setCopied] = useState<'markdown' | 'json' | null>(null);
  const origin = window.location.origin;
  const endpoint = `${origin}/mcp`;

  const config = `{
  "mcpServers": {
    "mc-librarian": {
      "type": "http",
      "url": "${endpoint}",
      "headers": { "Authorization": "Bearer <你的 mcl_ 令牌>" },
      "exposure": "direct",
      "timeout": 300
    }
  }
}`;

  const copy = async (kind: 'markdown' | 'json') => {
    try {
      await navigator.clipboard.writeText(kind === 'json' ? config : buildMarkdown(origin));
      setCopied(kind);
      window.setTimeout(() => setCopied(null), 2000);
    } catch {
      setCopied(null);
    }
  };

  return (
    <Card>
      <CardContent>
        <Stack direction="row" spacing={2} sx={{ alignItems: 'center', mb: 2 }}>
          <Typography variant="h6">MCP 接入</Typography>
          <Box sx={{ flex: '1 1 auto' }} />
          <Button size="small" variant="outlined" onClick={() => void copy('json')}>
            {copied === 'json' ? '已复制' : '复制配置'}
          </Button>
          <Button size="small" variant="contained" onClick={() => void copy('markdown')}>
            {copied === 'markdown' ? '已复制' : '复制接入说明'}
          </Button>
        </Stack>

        <Divider sx={{ mb: 2 }} />

        <Typography variant="body2" sx={{ color: 'text.secondary', mb: 2 }}>
          把检索能力以 <code>MCP</code>（streamable HTTP）暴露给外部 agent（pi / Claude Code /
          Cursor）。 用上面任一令牌的明文做 <code>Authorization: Bearer</code>，权限与当前账号一致。
          「复制接入说明」会生成一段 Markdown，可以直接发给你的 AI 让它自己配。
        </Typography>

        <Stack spacing={1.5}>
          <Box>
            <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
              服务地址
            </Typography>
            <CodeBlock>{endpoint}</CodeBlock>
          </Box>

          <Box>
            <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
              客户端配置
            </Typography>
            <CodeBlock>{config}</CodeBlock>
          </Box>

          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
            两个容易踩的点：<code>exposure</code> 要写 <code>direct</code>（pi 默认{' '}
            <code>codemode</code>
            会把工具注册但不告诉模型）；<code>timeout</code> 要 ≥300 秒（<code>ask</code> 单次可达
            90 秒）。
          </Typography>

          <Box>
            <Typography variant="subtitle2" sx={{ mb: 0.5 }}>
              可用工具（{TOOLS.length} 个）
            </Typography>
            <Stack spacing={0.5}>
              {TOOLS.map((tool) => (
                <Stack key={tool.name} direction="row" spacing={1} sx={{ alignItems: 'baseline' }}>
                  <Typography
                    variant="body2"
                    sx={{ fontFamily: 'monospace', flexShrink: 0, minWidth: 96 }}
                  >
                    {tool.name}
                  </Typography>
                  <Typography variant="body2" sx={{ color: 'text.secondary' }}>
                    {tool.what}
                  </Typography>
                </Stack>
              ))}
            </Stack>
          </Box>

          <Typography variant="caption" sx={{ color: 'text.secondary' }}>
            先调 <code>versions</code> 拿版本串——像 <code>forge-47.4.16+1.20.1</code>{' '}
            这种是模型猜不出来的。
          </Typography>
        </Stack>
      </CardContent>
    </Card>
  );
}

/** 等宽只读代码块：长行横向可滚，不撑破卡片。 */
function CodeBlock({ children }: { children: string }) {
  return (
    <Box
      component="pre"
      sx={{
        m: 0,
        p: 1.5,
        borderRadius: 1,
        bgcolor: 'background.neutral',
        fontFamily: 'monospace',
        fontSize: 12.5,
        lineHeight: 1.6,
        overflowX: 'auto',
        whiteSpace: 'pre',
      }}
    >
      {children}
    </Box>
  );
}
