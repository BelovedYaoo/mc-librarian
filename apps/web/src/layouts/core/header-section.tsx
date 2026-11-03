import { styled } from '@mui/material/styles';

import { mergeClasses } from 'src/utils/merge-classes';

import { layoutClasses } from './classes';

// ----------------------------------------------------------------------

export type HeaderSectionProps = React.ComponentProps<'header'>;

export function HeaderSection({ children, className, ...other }: HeaderSectionProps) {
  return (
    <HeaderRoot className={mergeClasses([layoutClasses.header, className])} {...other}>
      {children}
    </HeaderRoot>
  );
}

// ----------------------------------------------------------------------

const HeaderRoot = styled('header')(({ theme }) => ({
  display: 'flex',
  alignItems: 'center',
  minHeight: 64,
  paddingInline: theme.spacing(3),
  borderBottom: `1px solid ${theme.palette.divider}`,
  backgroundColor: theme.palette.background.paper,
}));
