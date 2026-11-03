import type { ThemeOptions } from '@mui/material/styles';

import { createTheme as createMuiTheme } from '@mui/material/styles';

// ----------------------------------------------------------------------

const baseOptions: ThemeOptions = {
  palette: {
    mode: 'light',
    primary: { main: '#1f6feb' },
    background: { default: '#f4f6f8', paper: '#ffffff' },
  },
  shape: { borderRadius: 8 },
  typography: {
    fontFamily: [
      '"Public Sans"',
      '"Noto Sans SC"',
      'system-ui',
      '-apple-system',
      '"Segoe UI"',
      'sans-serif',
    ].join(','),
    h4: { fontWeight: 700 },
    h6: { fontWeight: 600 },
  },
};

export function createTheme(options: ThemeOptions = {}) {
  return createMuiTheme({
    ...baseOptions,
    ...options,
    palette: { ...baseOptions.palette, ...options.palette },
  });
}
