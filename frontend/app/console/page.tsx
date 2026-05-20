'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { API_BASE, logout } from '@/lib/api';
import type { UserContext } from '@/lib/types';

const DEFAULT_TENANT = process.env.NEXT_PUBLIC_DEFAULT_TENANT || 'security';

/**
 * 관리자 콘솔 entry — /console (산업 표준: AWS console / Azure portal 패턴).
 *
 * 동작:
 *   - 미인증: 즉시 SSO redirect (default tenant)
 *   - PLATFORM_ADMIN: /platform/admin/tenants
 *   - ADMIN: /{tenant}/admin/dashboard
 *   - USER role(권한 없음): "다른 계정으로 로그인" + "채팅으로 돌아가기" UI
 *     → 다른 계정 로그인은 logout 후 SSO 재시작 (같은 브라우저에서 계정 전환)
 */
export default function ConsoleEntry() {
  const router = useRouter();
  const [user, setUser] = useState<UserContext | null>(null);
  const [isLoading, setIsLoading] = useState(true);
  const [switching, setSwitching] = useState(false);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/auth/me`, { credentials: 'include' })
      .then(async (res) => {
        if (cancelled) return;
        if (res.status === 401) {
          window.location.href = `${API_BASE}/api/auth/authorize/${DEFAULT_TENANT}?redirect=1`;
          return;
        }
        if (!res.ok) return;
        const body = (await res.json()) as UserContext;
        setUser(body);
        if (body.is_platform_admin) {
          router.replace('/platform/admin/tenants');
        } else if (body.is_admin) {
          router.replace(`/${body.tenant_id}/admin/dashboard`);
        }
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  const switchAccount = async () => {
    if (!user) return;
    setSwitching(true);
    try {
      await logout(user.tenant_id);
    } catch {
      // 실패해도 SSO 재시작은 진행
    }
    window.location.href = `${API_BASE}/api/auth/authorize/${user.tenant_id}?redirect=1`;
  };

  if (isLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-gray-50 text-sm text-gray-500">
        권한 확인 중…
      </main>
    );
  }

  if (user && !user.is_admin && !user.is_platform_admin) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-gray-50 p-8">
        <div className="w-full max-w-md bg-white border border-gray-200 rounded-2xl p-8 shadow-sm">
          <h1 className="text-xl font-semibold text-gray-900 mb-1">
            관리자 콘솔
          </h1>
          <p className="text-sm text-gray-500 mb-6">
            이 계정에는 관리자 권한이 없습니다.
          </p>

          <div className="bg-gray-50 border border-gray-200 rounded-lg px-3 py-2 mb-6 text-xs">
            <div className="flex items-center justify-between">
              <span className="text-gray-500">현재 로그인</span>
              <span className="font-medium text-gray-900 truncate ml-2">
                {user.preferred_username ?? user.email ?? user.user_id}
              </span>
            </div>
            <div className="flex items-center justify-between mt-1">
              <span className="text-gray-500">권한</span>
              <span className="text-gray-700">
                {user.roles.join(', ') || 'USER'}
              </span>
            </div>
          </div>

          <div className="space-y-2">
            <button
              onClick={switchAccount}
              disabled={switching}
              className="w-full px-4 py-2.5 bg-gray-900 text-white rounded-lg text-sm font-medium hover:bg-gray-800 disabled:bg-gray-400 transition-colors"
            >
              {switching ? '로그아웃 중…' : '다른 계정으로 로그인'}
            </button>
            <Link
              href={`/${user.tenant_id}/chat`}
              className="block w-full text-center px-4 py-2.5 border border-gray-300 rounded-lg text-sm text-gray-700 hover:bg-gray-50 transition-colors"
            >
              채팅으로 돌아가기
            </Link>
          </div>

          <p className="text-[11px] text-gray-400 mt-6 leading-relaxed">
            관리자 권한이 있는 계정으로 다시 로그인하면 자동으로 콘솔에
            진입합니다.
          </p>
        </div>
      </main>
    );
  }

  // admin/platform_admin은 useEffect에서 이미 redirect. 도달 직전 일시 화면.
  return (
    <main className="min-h-screen flex items-center justify-center bg-gray-50 text-sm text-gray-500">
      콘솔로 이동 중…
    </main>
  );
}
