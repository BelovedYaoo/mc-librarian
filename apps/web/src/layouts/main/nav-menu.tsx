import Box from '@mui/material/Box';
import List from '@mui/material/List';
import ListItemButton from '@mui/material/ListItemButton';
import ListItemText from '@mui/material/ListItemText';
import { useLocation, useNavigate } from 'react-router';

import { useAuth } from 'src/auth';

import { paths } from 'src/routes/paths';

// ----------------------------------------------------------------------

type NavItem = {
  label: string;
  to: string;
  /** 只对 admin 显示；后端每个 `/v1/admin/*` 仍会独立校验。 */
  adminOnly?: boolean;
};

const NAV_ITEMS: NavItem[] = [
  { label: '概览', to: paths.home },
  { label: '版本', to: paths.versions },
  { label: '任务', to: paths.jobs, adminOnly: true },
];

/** 侧边导航：版本与任务（#4），后续 ticket 继续往里加。 */
export function NavMenu() {
  const { user } = useAuth();
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const items = NAV_ITEMS.filter((item) => !item.adminOnly || user?.role === 'admin');

  return (
    <Box
      component="nav"
      sx={{
        width: 180,
        flex: '0 0 auto',
        p: 2,
        borderRight: 1,
        borderColor: 'divider',
        display: { xs: 'none', md: 'block' },
      }}
    >
      <List disablePadding>
        {items.map((item) => (
          <ListItemButton
            key={item.to}
            selected={pathname === item.to}
            onClick={() => navigate(item.to)}
            sx={{ borderRadius: 1 }}
          >
            <ListItemText primary={item.label} />
          </ListItemButton>
        ))}
      </List>
    </Box>
  );
}
