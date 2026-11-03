import type { CSSObject, SxProps, Theme } from '@mui/material/styles';

import GlobalStyles from '@mui/material/GlobalStyles';
import { styled } from '@mui/material/styles';

import { mergeClasses } from 'src/utils/merge-classes';

import { layoutClasses } from './classes';

// ----------------------------------------------------------------------

export type LayoutSectionProps = React.ComponentProps<'div'> & {
  sx?: SxProps<Theme>;
  cssVars?: CSSObject;
  children?: React.ReactNode;
  headerSection?: React.ReactNode;
};

export function LayoutSection({
  sx,
  cssVars,
  children,
  headerSection,
  className,
  ...other
}: LayoutSectionProps) {
  return (
    <>
      <GlobalStyles styles={(theme) => ({ body: { ...baseVars(theme), ...cssVars } })} />

      <LayoutRoot
        id={layoutClasses.root}
        data-layout="main"
        className={mergeClasses([layoutClasses.root, className])}
        sx={sx}
        {...other}
      >
        {headerSection}
        {children}
      </LayoutRoot>
    </>
  );
}

// ----------------------------------------------------------------------

const LayoutRoot = styled('div')(({ theme }) => ({
  display: 'flex',
  flex: '1 1 auto',
  minHeight: '100dvh',
  flexDirection: 'column',
  backgroundColor: theme.palette.background.default,
}));

function baseVars(theme: Theme): CSSObject {
  return {
    margin: 0,
    backgroundColor: theme.palette.background.default,
  };
}
