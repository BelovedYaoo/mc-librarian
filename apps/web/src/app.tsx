import 'src/global.css';

import { AuthProvider } from 'src/auth';
import { ThemeProvider } from 'src/theme';

// ----------------------------------------------------------------------

type AppProps = {
  children: React.ReactNode;
};

export default function App({ children }: AppProps) {
  return (
    <ThemeProvider>
      <AuthProvider>{children}</AuthProvider>
    </ThemeProvider>
  );
}
