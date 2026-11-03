import Typography from '@mui/material/Typography';
import Container from '@mui/material/Container';
import Button from '@mui/material/Button';
import Box from '@mui/material/Box';
import { isRouteErrorResponse, useNavigate, useRouteError } from 'react-router';

// ----------------------------------------------------------------------

export function ErrorBoundary() {
  const error = useRouteError();
  const navigate = useNavigate();

  const message = isRouteErrorResponse(error)
    ? `${error.status} ${error.statusText}`
    : error instanceof Error
      ? error.message
      : '未知错误';

  return (
    <Container sx={{ py: 10 }}>
      <Typography variant="h4" sx={{ mb: 2 }}>
        页面出错了
      </Typography>
      <Typography variant="body2" sx={{ mb: 4, color: 'text.secondary' }}>
        {message}
      </Typography>
      <Box>
        <Button variant="contained" onClick={() => navigate(0)}>
          重新加载
        </Button>
      </Box>
    </Container>
  );
}
