/**
 * Platform Admin Layout — /platform/admin/* (ADR-008 + ADR-017 §18).
 *
 * RBAC: client-side에서 user.is_platform_admin 확인 후 미달이면 홈으로 redirect.
 * backend는 require_platform_admin가 다시 403으로 막는다 (이중 방어).
 */
'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect } from 'react';
import useSWR from 'swr';
import { logout, swrFetcher } from '@/lib/api';
import type { UserContext } from '@/lib/types';

const MENU = [
  { label: 'Tenants', href: '/platform/admin/tenants' },
  { label: 'Endpoints', href: '/platform/admin/endpoints' },
  { label: 'Health Metrics', href: '/platform/admin/health' },
  { label: 'Usage Analytics', href: '/platform/admin/analytics/usage' },
  { label: 'Platform Configs', href: '/platform/admin/configs' },
];

export default function PlatformAdminLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  const pathname = usePathname();
  const router = useRouter();

  const { data: user, isLoading } = useSWR<UserContext>(
    '/api/auth/me',
    swrFetcher,
  );

  useEffect(() => {
    if (!isLoading && user && !user.is_platform_admin) {
      router.replace('/');
    }
  }, [user, isLoading, router]);

  if (isLoading || !user) {
    return <div className="p-6 text-sm text-gray-500">권한 확인 중…</div>;
  }
  if (!user.is_platform_admin) {
    return <div className="p-6 text-sm text-gray-500">권한 없음 — 이동 중…</div>;
  }

  return (
    <div className="flex h-screen bg-gray-50 font-sans">
      <aside className="w-64 border-r border-gray-200 bg-white flex flex-col">
        <div className="px-4 py-3.5 border-b border-gray-200">
          <div className="flex items-center gap-2">
            <div className="w-7 h-7 rounded-lg bg-brand-600 text-white flex items-center justify-center text-xs font-bold">
              🌐
            </div>
            <p className="font-semibold text-sm text-gray-900">Platform</p>
          </div>
          <p className="text-[10px] text-gray-500 mt-2">
            PLATFORM_ADMIN 한정 · cross-tenant
          </p>
        </div>
        <nav className="flex-1 overflow-y-auto py-2 px-1.5">
          {MENU.map((m) => {
            const active = pathname?.startsWith(m.href);
            return (
              <Link
                key={m.href}
                href={m.href}
                className={`flex items-center px-3 py-1.5 my-0.5 rounded-md text-sm transition-colors ${
                  active
                    ? 'bg-gray-900 text-white font-medium'
                    : 'text-gray-700 hover:bg-gray-100'
                }`}
              >
                {m.label}
              </Link>
            );
          })}
        </nav>
        <div className="p-2 border-t border-gray-200 space-y-0.5">
          <Link
            href="/"
            className="block w-full px-3 py-1.5 rounded-md text-xs text-gray-700 hover:bg-gray-100"
          >
            ← 홈
          </Link>
          <Link
            href="/me"
            className="block w-full px-3 py-1.5 rounded-md text-xs text-gray-700 hover:bg-gray-100"
          >
            내 계정
          </Link>
          {user && (
            <button
              onClick={async () => {
                await logout(user.tenant_id);
                window.location.href = '/';
              }}
              className="block w-full text-left px-3 py-1.5 rounded-md text-xs text-gray-500 hover:bg-gray-100 hover:text-gray-700"
            >
              로그아웃
            </button>
          )}
        </div>
      </aside>
      <main className="flex-1 overflow-y-auto">{children}</main>
    </div>
  );
}
