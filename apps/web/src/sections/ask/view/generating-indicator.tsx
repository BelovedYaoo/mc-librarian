/**
 * 生成中的视觉反馈（移植自 beautifului.dev 的 `Loading State`，改成 MUI + `sx`）。
 *
 * 那边是 Tailwind 组件（`className="grid grid-cols-[repeat(3,4px)]"` + `bg-ink` 自定义 token），
 * 本项目是 MUI + `sx`，装不了它的注册表；所以这里只**搬设计**：3×3 像素格按对角线错峰点亮
 * （650ms 一轮、每格间隔 90ms），配一个 shimmer 文字和等宽的实时计时。
 *
 * 为什么不用 `CircularProgress`：转圈只表达「在等」，像素格 + 实时秒数表达的是「在做事，已经
 * N 秒」——问答动辄几十秒，可见的进度比一个永不停的圈更有用。
 */

import { keyframes } from '@mui/material/styles';
import Box from '@mui/material/Box';
import Stack from '@mui/material/Stack';

import { useElapsed } from './use-elapsed';

// 单格亮起的动画：只改 opacity，不改尺寸（避免每帧重排）。
const pixelOn = keyframes`
  0%, 100% { opacity: 0.15; }
  50% { opacity: 1; }
`;

// 文字微光扫过：background-clip:text 的经典写法，不额外加遮蔽层。
const shimmer = keyframes`
  0% { background-position: 200% 0; }
  100% { background-position: -200% 0; }
`;

/**
 * 每个格子的延迟：按「列 + |行 - 1|」排成人字形波前（从中间一行向右推进）。
 * 这是 beautifului 的 `chevron` 序列，逐值相同：`[90, 180, 270, 0, 90, 180, 90, 180, 270]`。
 */
const CHEVRON_DELAYS = Array.from({ length: 9 }, (_, index) => {
  const row = Math.floor(index / 3);
  const column = index % 3;

  return (column + Math.abs(row - 1)) * 90;
});

/** 3×3 像素格。`aria-hidden`：它是装饰，读屏应当只读旁边的文字。 */
function PixelGrid() {
  return (
    <Box
      aria-hidden
      sx={{
        display: 'grid',
        gridTemplateColumns: 'repeat(3, 4px)',
        gap: '1.5px',
        flexShrink: 0,
      }}
    >
      {CHEVRON_DELAYS.map((delay, index) => (
        <Box
          key={index}
          sx={{
            width: 4,
            height: 4,
            borderRadius: '1px',
            bgcolor: 'primary.main',
            animation: `${pixelOn} 650ms ease-in-out ${delay}ms infinite`,
            // 尊重系统的「减少动态效果」：停在下限亮度，计时照走。
            '@media (prefers-reduced-motion: reduce)': { animation: 'none', opacity: 0.15 },
          }}
        />
      ))}
    </Box>
  );
}

export type GeneratingIndicatorProps = {
  /** 计时起点（`Date.now()`）；`null` 表示还没开始。 */
  startedAt: number | null;
  label?: string;
};

/** 模型还没吐出第一个字时的占位。有增量文本之后就该换成一闪一闪的光标，不要叠着两个动画。 */
export function GeneratingIndicator({
  startedAt,
  label = '正在生成回答',
}: GeneratingIndicatorProps) {
  const elapsed = useElapsed(startedAt);

  return (
    <>
      <Stack direction="row" spacing={1.25} sx={{ alignItems: 'center', py: 0.5 }}>
        <PixelGrid />

        <Box
          component="span"
          sx={(theme) => ({
            fontSize: 14,
            backgroundImage: `linear-gradient(90deg, ${theme.palette.text.secondary} 0%, ${theme.palette.text.primary} 50%, ${theme.palette.text.secondary} 100%)`,
            backgroundSize: '200% 100%',
            backgroundClip: 'text',
            WebkitBackgroundClip: 'text',
            color: 'transparent',
            animation: `${shimmer} 1.8s linear infinite`,
            '@media (prefers-reduced-motion: reduce)': {
              animation: 'none',
              color: 'text.secondary',
              backgroundImage: 'none',
            },
          })}
        >
          {label}
        </Box>

        {/* 等宽 + 等宽数字：秒数跳动时不挤动右边的文字。 */}
        <Box
          component="span"
          sx={{
            fontFamily: 'monospace',
            fontVariantNumeric: 'tabular-nums',
            fontSize: 13,
            color: 'text.secondary',
          }}
        >
          {formatSeconds(elapsed)}
        </Box>
      </Stack>

      {/* 骨架行：只有一行「正在生成回答」时，正文区是一大片空白，容易被读成卡死。
        三条宽度递减的微光条给出「内容马上就来」的形状预期；出现第一个字就整块消失。 */}
      <Stack spacing={1} sx={{ mt: 1.5 }} aria-hidden>
        {['100%', '92%', '55%'].map((width) => (
          <Box
            key={width}
            sx={{
              height: 12,
              width,
              borderRadius: 0.5,
              bgcolor: 'action.hover',
              backgroundImage: (theme) =>
                `linear-gradient(90deg, transparent 0%, ${theme.palette.action.selected} 50%, transparent 100%)`,
              backgroundSize: '200% 100%',
              animation: `${shimmer} 1.6s linear infinite`,
              '@media (prefers-reduced-motion: reduce)': { animation: 'none' },
            }}
          />
        ))}
      </Stack>
    </>
  );
}

/** 秒数格式：10 秒以内给一位小数（看得出在动），之后取整（少跳）。 */
export function formatSeconds(ms: number): string {
  const seconds = ms / 1000;

  return seconds < 10 ? `${seconds.toFixed(1)}s` : `${Math.round(seconds)}s`;
}
