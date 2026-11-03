import { useLocation } from 'react-router';

// ----------------------------------------------------------------------

export function usePathname() {
  const { pathname } = useLocation();

  return pathname;
}
