'use client';

import Link from 'next/link';
import { usePathname, useRouter } from 'next/navigation';
import { useEffect } from 'react';
import useSWR from 'swr';
import { logout, swrFetcher } from '@/lib/api';
import type { UserContext } from '@/lib/types';

const NAV = [
  { href: '/account', label: '프로필', exact: true },
  { href: '/account/security', label: '보안' },
  { href: '/account/sessions', label: '세션' },
];

export default function AccountLayout({ children }: { children: React.ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  const { data: user, isLoading } = useSWR<UserContext>('/api/auth/me', swrFetcher, {
    shouldRetryOnError: false,
  });

  useEffect(() => {
    if (!isLoading && !user) router.replace('/');
  }, [user, isLoading, router]);

  if (isLoading || !user) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-gray-50 text-sm text-gray-500">
        로딩 중…
      </main>
    );
  }

  const initial = (user.preferred_username ?? user.email ?? user.user_id)
    .charAt(0)
    .toUpperCase();

  return (
    <div className="min-h-screen bg-gray-50 font-sans antialiased">
      <header className="bg-white border-b border-gray-200 sticky top-0 z-10">
        <div className="max-w-5xl mx-auto px-6 h-14 flex items-center justify-between">
          <div className="flex items-center gap-3">
            <Link
              href={`/${user.tenant_id}/chat`}
              className="text-sm text-gray-600 hover:text-gray-900 flex items-center gap-1"
            >
              ← 채팅
            </Link>
            <div className="h-4 w-px bg-gray-200" />
            <h1 className="text-sm font-semibold text-gray-900">내 계정</h1>
          </div>
          <button
            onClick={async () => {
              await logout(user.tenant_id);
              window.location.href = '/';
            }}
            className="text-xs text-gray-500 hover:text-gray-900"
          >
            로그아웃
          </button>
        </div>
      </header>

      <div className="max-w-5xl mx-auto px-6 py-8">
        <div className="flex items-center gap-4 mb-8">
          <div className="w-14 h-14 rounded-full bg-gray-900 text-white flex items-center justify-center text-lg font-semibold">
            {initial}
          </div>
          <div>
            <div className="text-base font-semibold text-gray-900">
              {user.preferred_username ?? user.email ?? user.user_id}
            </div>
            <div className="text-xs text-gray-500 mt-0.5">
              {user.email ?? '(이메일 없음)'} · {user.tenant_id} ·{' '}
              {user.roles.join(', ') || 'USER'}
            </div>
          </div>
        </div>

        <nav className="border-b border-gray-200 mb-6">
          <div className="flex gap-1">
            {NAV.map((item) => {
              const active = item.exact
                ? pathname === item.href
                : pathname?.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`px-4 py-2.5 text-sm font-medium border-b-2 transition-colors ${
                    active
                      ? 'border-gray-900 text-gray-900'
                      : 'border-transparent text-gray-500 hover:text-gray-900'
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </div>
        </nav>

        <div className="animate-fade-in">{children}</div>
      </div>
    </div>
  );
}
