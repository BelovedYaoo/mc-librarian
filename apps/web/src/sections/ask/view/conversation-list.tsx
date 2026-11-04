import type { ConversationSummary } from 'src/lib/api-client';

import { useState } from 'react';
import Alert from '@mui/material/Alert';
import Button from '@mui/material/Button';
import CircularProgress from '@mui/material/CircularProgress';
import List from '@mui/material/List';
import ListItem from '@mui/material/ListItem';
import ListItemButton from '@mui/material/ListItemButton';
import ListItemText from '@mui/material/ListItemText';
import Stack from '@mui/material/Stack';
import Typography from '@mui/material/Typography';

// ----------------------------------------------------------------------

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
/** 超过这个天数就不再说「x 天前」，直接给日期。 */
const RELATIVE_LIMIT_DAYS = 30;

/** 左栏一项的更新时间：最近的说「x 分钟/小时/天前」，更早的给本地日期。 */
function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();

  if (Number.isNaN(then)) return '';

  const diff = Date.now() - then;

  if (diff < MINUTE_MS) return '刚刚';
  if (diff < HOUR_MS) return `${Math.floor(diff / MINUTE_MS)} 分钟前`;
  if (diff < DAY_MS) return `${Math.floor(diff / HOUR_MS)} 小时前`;
  if (diff < RELATIVE_LIMIT_DAYS * DAY_MS) return `${Math.floor(diff / DAY_MS)} 天前`;

  return new Date(then).toLocaleDateString();
}

/**
 * 问答页左栏：历史会话列表。
 *
 * 顶部「新建会话」清空主区（**不调接口**：首次提问时后端自动建会话）；点历史项载入该会话；
 * hover 时出现删除按钮；当前会话高亮。窄屏由调用方把同一个组件放进抽屉。
 */
export function ConversationList({
  conversations,
  activeId,
  loading,
  error,
  onSelect,
  onCreate,
  onDelete,
  onRefresh,
}: {
  conversations: ConversationSummary[];
  activeId: string | null;
  loading: boolean;
  error: string | null;
  onSelect: (id: string) => void;
  onCreate: () => void;
  onDelete: (id: string) => Promise<void>;
  onRefresh: () => void;
}) {
  const [busyId, setBusyId] = useState<string | null>(null);

  const handleDelete = async (id: string) => {
    setBusyId(id);

    try {
      await onDelete(id);
    } finally {
      setBusyId(null);
    }
  };

  return (
    <Stack spacing={1.5} sx={{ height: '100%', minHeight: 0 }}>
      <Stack direction="row" spacing={1} sx={{ alignItems: 'center' }}>
        <Typography variant="subtitle2" sx={{ flex: '1 1 auto' }}>
          历史会话
        </Typography>

        <Button size="small" onClick={onRefresh} disabled={loading}>
          刷新
        </Button>
      </Stack>

      <Button variant="outlined" size="small" onClick={onCreate}>
        新建会话
      </Button>

      {error !== null && <Alert severity="error">{error}</Alert>}

      {loading && conversations.length === 0 && (
        <Stack sx={{ alignItems: 'center', py: 3 }}>
          <CircularProgress size={20} />
        </Stack>
      )}

      {!loading && conversations.length === 0 && (
        <Typography variant="body2" sx={{ color: 'text.secondary' }}>
          还没有历史会话，问一次就会出现在这里。
        </Typography>
      )}

      <List dense disablePadding sx={{ flex: '1 1 auto', minHeight: 0, overflowY: 'auto' }}>
        {conversations.map((item) => (
          <ListItem
            key={item.id}
            disablePadding
            sx={{
              '& .conversation-delete': { opacity: 0 },
              '&:hover .conversation-delete': { opacity: 1 },
              '&:focus-within .conversation-delete': { opacity: 1 },
            }}
            secondaryAction={
              <Button
                className="conversation-delete"
                size="small"
                color="error"
                disabled={busyId === item.id}
                onClick={() => void handleDelete(item.id)}
                sx={{ minWidth: 0, px: 0.5 }}
              >
                删除
              </Button>
            }
          >
            <ListItemButton
              selected={item.id === activeId}
              onClick={() => onSelect(item.id)}
              sx={{ borderRadius: 1, pr: 7 }}
            >
              <ListItemText
                primary={item.title}
                secondary={relativeTime(item.updated_at)}
                slotProps={{
                  primary: { noWrap: true, variant: 'body2' },
                  secondary: { variant: 'caption' },
                }}
              />
            </ListItemButton>
          </ListItem>
        ))}
      </List>
    </Stack>
  );
}
