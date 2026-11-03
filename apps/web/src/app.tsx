import 'src/global.css';

import { ThemeProvider } from 'src/theme';

// ----------------------------------------------------------------------

type AppProps = {
  children: React.ReactNode;
};

export default function App({ children }: AppProps) {
  return <ThemeProvider>{children}</ThemeProvider>;
}
