import { defineConfig } from 'vitest/config'

export default defineConfig({
  esbuild: {
    jsx: 'automatic',
  },
  resolve: {
    dedupe: [
      '@tanstack/react-query',
      '@testing-library/react',
      'lucide-react',
      'react',
      'react-dom',
      'react-router-dom',
    ],
  },
  server: {
    fs: { allow: ['..'] },
  },
  test: {
    environment: 'jsdom',
    include: ['../test/frontend/**/*.test.ts', '../test/frontend/**/*.test.tsx'],
  },
})
