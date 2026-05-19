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
import { swrFetcher } from '@/lib/api';
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
    <div className="flex h-screen">
      <aside className="w-64 border-r border-gray-200 bg-gray-50 flex flex-col">
        <div className="p-4 border-b border-gray-200">
          <p className="font-bold">🌐 Platform Admin</p>
          <p className="text-xs text-gray-500 mt-1">
            PLATFORM_ADMIN role 한정
          </p>
        </div>
        <nav className="flex-1 py-2">
          {MENU.map((m) => {
            const active = pathname?.startsWith(m.href);
            return (
              <Link
                key={m.href}
                href={m.href}
                className={`block px-4 py-2 text-sm ${
                  active
                    ? 'bg-blue-100 text-blue-900 border-l-4 border-blue-600'
                    : 'text-gray-700 hover:bg-gray-100'
                }`}
              >
                {m.label}
              </Link>
            );
          })}
        </nav>
        <div className="p-3 border-t">
          <Link href="/" className="text-xs text-blue-600 hover:underline">
            ← 홈
          </Link>
        </div>
      </aside>
      <main className="flex-1 overflow-y-auto bg-white">{children}</main>
    </div>
  );
}
