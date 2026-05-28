'use client';

import React, {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
} from 'react';
import { translations, type Locale } from '@/lib/i18n';

type Dict = Record<string, unknown>;

function getNested(obj: Dict, path: string): unknown {
  let cur: unknown = obj;
  for (const p of path.split('.')) cur = (cur as Dict)?.[p];
  return cur;
}

type TFunc = (key: string) => string;

const LOCALE_KEY = 'domainrag_locale';

const LocaleContext = createContext<{
  locale: Locale;
  setLocale: (l: Locale) => void;
  t: TFunc;
} | null>(null);

export function LanguageProvider({ children }: { children: React.ReactNode }) {
  const [locale, setLocaleState] = useState<Locale>('ko');

  // 마운트 시 저장된 locale 복원 (SSR-safe).
  useEffect(() => {
    try {
      const v = localStorage.getItem(LOCALE_KEY);
      if (v === 'ko' || v === 'en') setLocaleState(v);
    } catch {
      /* localStorage 차단 환경 — 기본 ko */
    }
  }, []);

  const setLocale = useCallback((l: Locale) => {
    setLocaleState(l);
    try {
      localStorage.setItem(LOCALE_KEY, l);
    } catch {
      /* empty */
    }
  }, []);

  const t = useCallback<TFunc>(
    (key: string) => {
      const value = getNested(translations[locale] as Dict, key);
      return typeof value === 'string' ? value : key;
    },
    [locale],
  );

  const value = useMemo(() => ({ locale, setLocale, t }), [locale, setLocale, t]);
  return <LocaleContext.Provider value={value}>{children}</LocaleContext.Provider>;
}

export function useLanguage() {
  const ctx = useContext(LocaleContext);
  if (!ctx) throw new Error('useLanguage must be used within LanguageProvider');
  return ctx;
}
