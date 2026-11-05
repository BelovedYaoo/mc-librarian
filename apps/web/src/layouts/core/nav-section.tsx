import { styled } from '@mui/material/styles';
import { NavLink } from 'react-router';

import { mergeClasses } from 'src/utils/merge-classes';

import { layoutClasses } from './classes';

// ----------------------------------------------------------------------

export type NavItem = {
  label: string;
  /** 路由路径；与 `paths` 里的常量对应。 */
  to: string;
  /** 是否只在完全匹配时高亮（首页需要，子路径页不需要）。 */
  end?: boolean;
};

export type NavSectionProps = {
  items: NavItem[];
};

/**
 * 侧边导航：宽屏在左、窄屏横排在内容上方。
 * 条目由调用方（`MainLayout`）按当前用户角色过滤——这里只管画。
 */
export function NavSection({ items }: NavSectionProps) {
  return (
    <NavRoot className={mergeClasses([layoutClasses.nav])}>
      {items.map((item) => (
        <NavItemLink key={item.to} to={item.to} end={item.end}>
          {item.label}
        </NavItemLink>
      ))}
    </NavRoot>
  );
}

// ----------------------------------------------------------------------

const NavRoot = styled('nav')(({ theme }) => ({
  display: 'flex',
  gap: theme.spacing(1),
  padding: theme.spacing(2),
  // 导航列就是白底（`background.default`）：Minimal 的 `--layout-nav-bg` 也是
  // `palette.background.default`，light 下即 `#FFFFFF`。层次靠右边框，不靠灰底。
  backgroundColor: theme.palette.background.default,
  borderBottom: `1px solid ${theme.palette.divider}`,
  [theme.breakpoints.up('md')]: {
    flex: '0 0 180px',
    flexDirection: 'column',
    borderBottom: 'none',
    borderRight: `1px solid ${theme.palette.divider}`,
  },
}));

const NavItemLink = styled(NavLink)(({ theme }) => ({
  padding: theme.spacing(1, 1.5),
  borderRadius: theme.shape.borderRadius,
  color: theme.palette.text.secondary,
  textDecoration: 'none',
  ...theme.typography.body2,
  '&:hover': { backgroundColor: theme.palette.action.hover },
  '&.active': {
    backgroundColor: theme.palette.action.selected,
    // 用 `primary.dark` 而不是 `primary.main`：新主色 #00A76F 在选中底色上只有 2.52:1，
    // `primary.dark`（#007867）在同一底色上是 4.39:1，绿色标识还在。
    color: theme.palette.primary.dark,
    fontWeight: theme.typography.fontWeightMedium,
    // 暗色下反过来：`primary.dark`（#007867）在 `action.selected` 上只有 2.40:1，
    // 换成 `primary.light`（#5BE49B）→ 8.05:1，绿色标识还在。
    ...theme.applyStyles('dark', { color: theme.palette.primary.light }),
  },
}));
