import { styled } from '@mui/material/styles';

import { mergeClasses } from 'src/utils/merge-classes';

import { layoutClasses } from './classes';

// ----------------------------------------------------------------------

export type MainSectionProps = React.ComponentProps<'main'>;

export function MainSection({ children, className, ...other }: MainSectionProps) {
  return (
    <MainRoot className={mergeClasses([layoutClasses.main, className])} {...other}>
      {children}
    </MainRoot>
  );
}

// ----------------------------------------------------------------------

const MainRoot = styled('main')({
  display: 'flex',
  flex: '1 1 auto',
  flexDirection: 'column',
});
