'use client';

import { ThemeProvider as NextThemesProvider } from 'next-themes';

// next-themes 래퍼 (WiSentinel 패턴). class 전략 — tailwind darkMode:'class'와 정합.
export function ThemeProvider({ children }: { children: React.ReactNode }) {
  return (
    <NextThemesProvider
      attribute="class"
      defaultTheme="light"
      enableSystem={false}
      disableTransitionOnChange
    >
      {children}
    </NextThemesProvider>
  );
}
