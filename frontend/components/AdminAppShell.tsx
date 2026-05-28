/**
 * AdminAppShell — DomainRAG 관리자 콘솔 chrome (ADR-016 보강, WiSentinel AppShell 골격).
 *
 * - 접이식 사이드바: localStorage(domainrag_sidebar_collapsed), w-56/w-16, hover 시
 *   우측 경계 floating pill 토글, 접힘 시 아이콘만 + title 툴팁.
 * - 상단바: 좌측 시스템 헬스 핑(/api/health/ready) / 우측 클러스터
 *   [도메인 스위처(맨 앞)] → role 배지 → 테마 → 한글|EN → 내 계정 → 사용자명 → 로그아웃.
 * - RBAC: admin/auditor/platform_admin만. 그 외 자기 도메인 chat으로 redirect.
 */
'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useTheme } from 'next-themes';
import { useCallback, useEffect, useState } from 'react';
import {
  LayoutDashboard,
  FileText,
  RefreshCw,
  FileCode2,
  MessageSquare,
  ScanSearch,
  Route,
  SquarePen,
  Boxes,
  FlaskConical,
  ClipboardList,
  Wand2,
  ListChecks,
  Globe,
  SlidersHorizontal,
  Building2,
  Server,
  BarChart3,
  HeartPulse,
  Sun,
  Moon,
  LogOut,
  UserCog,
  Activity,
  ChevronLeft,
  ChevronRight,
  type LucideIcon,
} from 'lucide-react';
import useSWR from 'swr';
import DomainSwitcher from '@/components/DomainSwitcher';
import { API_BASE, logout, swrFetcher } from '@/lib/api';
import { useLanguage } from '@/components/LanguageProvider';
import type { UserContext } from '@/lib/types';

const SIDEBAR_COLLAPSE_KEY = 'domainrag_sidebar_collapsed';
const APP_VERSION = 'v0.1.0';

type MenuItem = { href: string; labelKey: string; icon: LucideIcon };
type MenuGroup = { labelKey: string; items: MenuItem[] };

function domainMenu(base: string): MenuGroup[] {
  return [
    { labelKey: '', items: [{ href: `${base}/dashboard`, labelKey: 'nav.dashboard', icon: LayoutDashboard }] },
    {
      labelKey: 'nav.knowledge',
      items: [
        { href: `${base}/documents`, labelKey: 'nav.documents', icon: FileText },
        { href: `${base}/indexing`, labelKey: 'nav.indexing', icon: RefreshCw },
        { href: `${base}/schema`, labelKey: 'nav.schema', icon: FileCode2 },
      ],
    },
    {
      labelKey: 'nav.query',
      items: [
        { href: `${base}/logs/chat`, labelKey: 'nav.chatLogs', icon: MessageSquare },
        { href: `${base}/citation-inspector`, labelKey: 'nav.citationInspector', icon: ScanSearch },
      ],
    },
    {
      labelKey: 'nav.modelRouting',
      items: [
        { href: `${base}/routing`, labelKey: 'nav.routing', icon: Route },
        { href: `${base}/prompts`, labelKey: 'nav.prompts', icon: SquarePen },
        { href: `${base}/lora`, labelKey: 'nav.lora', icon: Boxes },
      ],
    },
    { labelKey: 'nav.evaluation', items: [{ href: `${base}/evaluation`, labelKey: 'nav.evaluationConsole', icon: FlaskConical }] },
    {
      labelKey: 'nav.assessment',
      items: [
        { href: `${base}/assessment/items`, labelKey: 'nav.itemBank', icon: ClipboardList },
        { href: `${base}/assessment/workbench`, labelKey: 'nav.workbench', icon: Wand2 },
        { href: `${base}/assessment/review-queue`, labelKey: 'nav.reviewQueue', icon: ListChecks },
      ],
    },
    {
      labelKey: 'nav.settings',
      items: [
        { href: `${base}/domains`, labelKey: 'nav.domains', icon: Globe },
        { href: `${base}/configs`, labelKey: 'nav.configs', icon: SlidersHorizontal },
      ],
    },
  ];
}

function platformMenu(): MenuGroup[] {
  const base = '/platform/admin';
  return [
    {
      labelKey: 'nav.platform',
      items: [
        { href: `${base}/tenants`, labelKey: 'nav.tenants', icon: Building2 },
        { href: `${base}/endpoints`, labelKey: 'nav.endpoints', icon: Server },
        { href: `${base}/analytics/usage`, labelKey: 'nav.usage', icon: BarChart3 },
        { href: `${base}/health`, labelKey: 'nav.health', icon: HeartPulse },
        { href: `${base}/configs`, labelKey: 'nav.platformConfigs', icon: SlidersHorizontal },
      ],
    },
  ];
}

function useCollapsed(): [boolean, (v: boolean) => void] {
  const [collapsed, setState] = useState(false);
  useEffect(() => {
    try {
      if (localStorage.getItem(SIDEBAR_COLLAPSE_KEY) === '1') setState(true);
    } catch {
      /* empty */
    }
  }, []);
  const set = useCallback((v: boolean) => {
    setState(v);
    try {
      localStorage.setItem(SIDEBAR_COLLAPSE_KEY, v ? '1' : '0');
    } catch {
      /* empty */
    }
  }, []);
  return [collapsed, set];
}

type ReadyHealth = { checks?: Record<string, string | boolean> };

export default function AdminAppShell({
  domainId,
  children,
}: {
  domainId: string;
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();
  const { theme, setTheme } = useTheme();
  const { locale, setLocale, t } = useLanguage();
  const [mounted, setMounted] = useState(false);
  const [collapsed, setCollapsed] = useCollapsed();
  const [showPlatform, setShowPlatform] = useState(false);
  const [health, setHealth] = useState<ReadyHealth | null>(null);
  useEffect(() => setMounted(true), []);

  const { data: user, isLoading } = useSWR<UserContext>('/api/auth/me', swrFetcher);
  const isPlatformAdmin = user?.is_platform_admin ?? false;
  const canAccess =
    !!user && (user.is_admin || user.is_platform_admin || user.is_auditor);

  // RBAC redirect
  useEffect(() => {
    if (!isLoading && user && !canAccess) router.replace(`/${domainId}/chat`);
  }, [isLoading, user, canAccess, router, domainId]);

  // 시스템 헬스 (60초) — /api/health/ready
  useEffect(() => {
    if (!canAccess) return;
    let cancelled = false;
    const check = () =>
      fetch(`${API_BASE}/api/health/ready`, { credentials: 'include' })
        .then((r) => r.json())
        .then((d) => {
          if (!cancelled) setHealth(d);
        })
        .catch(() => {
          if (!cancelled) setHealth(null);
        });
    check();
    const id = setInterval(check, 60_000);
    return () => {
      cancelled = true;
      clearInterval(id);
    };
  }, [canAccess]);

  if (isLoading || !user) {
    return <div className="p-6 text-sm text-gray-500">{t('common.loading')}</div>;
  }
  if (!canAccess) {
    return <div className="p-6 text-sm text-gray-500">권한 없음 — 이동 중…</div>;
  }

  const groups =
    showPlatform && isPlatformAdmin ? platformMenu() : domainMenu(`/${domainId}/admin`);

  const roleLabel = isPlatformAdmin
    ? { key: 'role.platformAdmin', cls: 'bg-brand-100 text-brand-700 dark:bg-brand-500/20 dark:text-brand-300' }
    : user.is_admin
      ? { key: 'role.admin', cls: 'bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300' }
      : user.is_auditor
        ? { key: 'role.auditor', cls: 'bg-purple-100 text-purple-700 dark:bg-purple-900/40 dark:text-purple-300' }
        : { key: 'role.user', cls: 'bg-slate-100 text-slate-700 dark:bg-slate-700 dark:text-slate-300' };

  const healthEntries = health?.checks
    ? ['db', 'qdrant', 'storage'].map((k) => ({
        label: k.toUpperCase(),
        ok: health.checks![k] === 'ok' || health.checks![k] === true,
        present: k in (health.checks as object),
      })).filter((e) => e.present)
    : [];

  const handleLogout = async () => {
    const url = await logout(domainId);
    window.location.href = url || '/';
  };

  return (
    <div className="flex min-h-screen bg-slate-50 dark:bg-slate-900">
      <aside
        className={`group/sidebar relative z-20 ${collapsed ? 'w-16' : 'w-56'} flex-shrink-0 border-r border-slate-200 bg-white transition-all duration-200 dark:border-slate-700 dark:bg-slate-800`}
      >
        <div className="sticky top-0 flex h-screen flex-col py-4">
          {/* 로고/도메인 식별 */}
          <div className="mb-4 flex items-center gap-2 overflow-hidden px-3">
            <div className="flex h-9 w-9 flex-shrink-0 items-center justify-center rounded-lg bg-brand-600 text-sm font-bold text-white">
              {showPlatform ? '🌐' : domainId.charAt(0).toUpperCase()}
            </div>
            {!collapsed && (
              <div className="min-w-0">
                <p className="truncate text-sm font-bold text-slate-800 dark:text-slate-100">
                  {showPlatform ? t('nav.platform') : domainId}
                </p>
                <p className="text-[10px] text-slate-400">DomainRAG Ops</p>
              </div>
            )}
          </div>

          {/* platform 토글 (platform_admin 한정) */}
          {isPlatformAdmin && !collapsed && (
            <button
              onClick={() => setShowPlatform((v) => !v)}
              className="mx-3 mb-2 rounded-md border border-slate-200 px-2 py-1 text-[11px] text-slate-500 hover:bg-slate-100 dark:border-slate-600 dark:text-slate-300 dark:hover:bg-slate-700"
            >
              {showPlatform ? '← 도메인 메뉴' : 'Platform 메뉴 ▾'}
            </button>
          )}

          <nav className="flex-1 space-y-0.5 overflow-y-auto px-2">
            {groups.map((g, gi) => (
              <div key={g.labelKey || `g${gi}`} className="mb-2">
                {g.labelKey && !collapsed && (
                  <p className="px-3 py-1 text-[10px] font-semibold uppercase tracking-wider text-slate-400 dark:text-slate-500">
                    {t(g.labelKey)}
                  </p>
                )}
                {g.labelKey && collapsed && (
                  <div className="my-2 mx-3 border-t border-slate-200 dark:border-slate-700" />
                )}
                {g.items.map((item) => {
                  const active = pathname?.startsWith(item.href);
                  const Icon = item.icon;
                  return (
                    <Link
                      key={item.href}
                      href={item.href}
                      title={collapsed ? t(item.labelKey) : undefined}
                      className={`flex items-center gap-3 rounded-lg px-3 py-2 text-sm font-medium transition-colors ${
                        active
                          ? 'bg-brand-100 text-brand-700 dark:bg-brand-500/20 dark:text-brand-300'
                          : 'text-slate-600 hover:bg-slate-100 hover:text-slate-900 dark:text-slate-300 dark:hover:bg-slate-700 dark:hover:text-slate-100'
                      }`}
                    >
                      <Icon className="h-5 w-5 flex-shrink-0" />
                      {!collapsed && <span className="truncate">{t(item.labelKey)}</span>}
                    </Link>
                  );
                })}
              </div>
            ))}
          </nav>

          {/* footer — 버전 + © */}
          <div className="border-t border-slate-200 px-3 py-3 dark:border-slate-700">
            {collapsed ? (
              <p className="text-center text-[10px] text-slate-400">{APP_VERSION}</p>
            ) : (
              <div className="space-y-0.5">
                <p className="text-[10px] text-slate-400">{APP_VERSION}</p>
                <p className="text-[10px] text-slate-400">© 2026 WissensBaum</p>
              </div>
            )}
          </div>

          {/* 접기/펴기 floating pill (hover 시 노출) */}
          <button
            type="button"
            onClick={() => setCollapsed(!collapsed)}
            className="absolute top-12 -right-3 z-30 flex h-6 w-6 items-center justify-center rounded-full border border-slate-300 bg-white text-slate-500 opacity-0 shadow-md transition-all duration-150 hover:scale-110 hover:border-brand-500 hover:bg-brand-600 hover:text-white group-hover/sidebar:opacity-100 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-300"
            aria-label={collapsed ? t('topbar.expand') : t('topbar.collapse')}
            title={collapsed ? t('topbar.expand') : t('topbar.collapse')}
          >
            {collapsed ? <ChevronRight className="h-3.5 w-3.5" /> : <ChevronLeft className="h-3.5 w-3.5" />}
          </button>
        </div>
      </aside>

      {/* Main */}
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="sticky top-0 z-10 flex h-14 items-center justify-end gap-2 border-b border-slate-200 bg-white px-4 dark:border-slate-700 dark:bg-slate-800">
          {/* 시스템 헬스 (좌측) */}
          {healthEntries.length > 0 && (
            <div className="mr-auto flex items-center gap-3">
              <div className="flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wider text-slate-400">
                <Activity className="h-3.5 w-3.5" />
                {t('topbar.system')}
              </div>
              {healthEntries.map((s) => (
                <div key={s.label} className="flex items-center gap-1.5">
                  <span
                    className={`inline-block h-2 w-2 rounded-full ${
                      s.ok ? 'bg-green-500' : 'bg-red-500 animate-pulse'
                    }`}
                  />
                  <span className="text-xs font-medium text-slate-600 dark:text-slate-300">
                    {s.label}
                  </span>
                </div>
              ))}
            </div>
          )}

          {/* 도메인 스위처 — 우측 클러스터 맨 앞 */}
          <DomainSwitcher domainId={domainId} section="admin" />

          {/* role 배지 */}
          <span
            className={`hidden md:inline-flex items-center gap-1 rounded-full px-2.5 py-0.5 text-[11px] font-medium ${roleLabel.cls}`}
          >
            <span className="inline-block h-1.5 w-1.5 rounded-full bg-current opacity-70" />
            {t(roleLabel.key)}
          </span>

          {/* 테마 토글 */}
          {mounted ? (
            <button
              type="button"
              onClick={() => setTheme(theme === 'dark' ? 'light' : 'dark')}
              className="rounded-lg p-2 text-slate-500 hover:bg-slate-100 hover:text-slate-700 dark:text-slate-400 dark:hover:bg-slate-700 dark:hover:text-slate-200"
              title={theme === 'dark' ? t('topbar.lightMode') : t('topbar.darkMode')}
              aria-label={theme === 'dark' ? t('topbar.lightMode') : t('topbar.darkMode')}
            >
              <Sun className="h-5 w-5 dark:hidden" />
              <Moon className="hidden h-5 w-5 dark:block" />
            </button>
          ) : (
            <span className="h-9 w-9 rounded-lg bg-slate-100 dark:bg-slate-700" aria-hidden />
          )}

          {/* 언어 토글 */}
          <div className="flex overflow-hidden rounded-lg border border-slate-200 dark:border-slate-600">
            <button
              type="button"
              onClick={() => setLocale('ko')}
              className={`px-2.5 py-1 text-xs ${locale === 'ko' ? 'bg-slate-200 font-medium dark:bg-slate-600' : 'hover:bg-slate-100 dark:hover:bg-slate-700'}`}
            >
              한글
            </button>
            <button
              type="button"
              onClick={() => setLocale('en')}
              className={`px-2.5 py-1 text-xs ${locale === 'en' ? 'bg-slate-200 font-medium dark:bg-slate-600' : 'hover:bg-slate-100 dark:hover:bg-slate-700'}`}
            >
              EN
            </button>
          </div>

          {/* 내 계정 */}
          <Link
            href="/account"
            className="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 bg-white px-3 py-1.5 text-sm font-medium text-slate-700 hover:bg-slate-50 dark:border-slate-600 dark:bg-slate-800 dark:text-slate-200 dark:hover:bg-slate-700"
            title={t('topbar.myAccount')}
          >
            <UserCog className="h-4 w-4" />
            <span className="hidden md:inline">{t('topbar.myAccount')}</span>
          </Link>

          {/* 사용자명 */}
          <span className="hidden max-w-[140px] truncate text-sm text-slate-600 dark:text-slate-300 md:inline">
            {user.preferred_username ?? user.email ?? user.user_id}
          </span>

          {/* 로그아웃 */}
          <button
            type="button"
            onClick={() => void handleLogout()}
            className="rounded-lg p-2 text-slate-500 hover:bg-red-50 hover:text-red-600 dark:text-slate-400 dark:hover:bg-red-900/20 dark:hover:text-red-400"
            aria-label={t('topbar.logout')}
            title={t('topbar.logout')}
          >
            <LogOut className="h-5 w-5" />
          </button>
        </header>

        <main className="flex min-h-0 flex-1 flex-col overflow-y-auto">{children}</main>
      </div>
    </div>
  );
}
