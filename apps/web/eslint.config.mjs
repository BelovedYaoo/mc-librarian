import { globalIgnores } from 'eslint/config';
import eslintJs from '@eslint/js';
import pluginImport from 'eslint-plugin-import';
import pluginReact from 'eslint-plugin-react';
import pluginReactHooks from 'eslint-plugin-react-hooks';
import pluginUnusedImports from 'eslint-plugin-unused-imports';
import globals from 'globals';
import eslintTs from 'typescript-eslint';

// ----------------------------------------------------------------------

/**
 * @config reactConfig
 * Core rules for React, React Hooks, and general JavaScript/TypeScript.
 */
const reactConfig = {
  plugins: {
    'react-hooks': pluginReactHooks,
  },
  settings: {
    react: { version: 'detect' },
  },
  rules: {
    ...pluginReactHooks.configs.recommended.rules,
    'func-names': 1,
    'object-shorthand': 1,
    'no-useless-rename': 1,
    'default-case-last': 2,
    'consistent-return': 2,
    'no-constant-condition': 1,
    'arrow-body-style': [2, 'as-needed', { requireReturnForObjectLiteral: false }],
    // --- react ---
    'react/jsx-key': 2,
    'react/prop-types': 0,
    'react/display-name': 0,
    'react/no-children-prop': 0,
    'react/jsx-boolean-value': 2,
    'react/self-closing-comp': 2,
    'react/react-in-jsx-scope': 0,
    'react/jsx-no-useless-fragment': [1, { allowExpressions: true }],
    'react/jsx-curly-brace-presence': [2, { props: 'never', children: 'never' }],
    // --- react hooks ---
    'react-hooks/refs': 0,
    'react-hooks/immutability': 0,
    'react-hooks/set-state-in-effect': 0,
    'react-hooks/preserve-manual-memoization': 0,
    // --- typescript ---
    '@typescript-eslint/no-shadow': 2,
    '@typescript-eslint/no-explicit-any': 0,
    '@typescript-eslint/consistent-type-imports': 1,
  },
};

/**
 * @config importConfig
 * Module resolution and import/export hygiene.
 */
const importConfig = {
  plugins: {
    import: pluginImport,
  },
  settings: {
    ...pluginImport.configs.typescript.settings,
    'import/resolver': {
      ...(pluginImport.configs.typescript.settings?.['import/resolver'] ?? {}),
      typescript: {
        project: './tsconfig.json',
      },
    },
  },
  rules: {
    ...pluginImport.configs.recommended.rules,
    'import/named': 0,
    'import/export': 0,
    'import/default': 0,
    'import/namespace': 0,
    'import/no-named-as-default': 0,
    'import/no-named-as-default-member': 0,
    'import/newline-after-import': 2,
    'import/no-cycle': 0,
  },
};

/**
 * @config unusedImportsConfig
 * Unused module / variable detection.
 */
const unusedImportsConfig = {
  plugins: {
    'unused-imports': pluginUnusedImports,
  },
  rules: {
    'no-unused-vars': 0,
    '@typescript-eslint/no-unused-vars': 0,
    'unused-imports/no-unused-imports': 1,
    'unused-imports/no-unused-vars': [
      1,
      {
        vars: 'all',
        varsIgnorePattern: '^_',
        args: 'after-used',
        argsIgnorePattern: '^_',
      },
    ],
  },
};

// ----------------------------------------------------------------------

const baseConfig = {
  files: ['**/*.{js,jsx,mjs,cjs,ts,tsx}'],
};

export default [
  globalIgnores(['dist/**', 'build/**', 'node_modules/**', 'src/types/api.d.ts']),
  {
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
  },
  eslintJs.configs.recommended,
  ...eslintTs.configs.recommended,
  pluginReact.configs.flat.recommended,
  baseConfig,
  reactConfig,
  importConfig,
  unusedImportsConfig,
];
