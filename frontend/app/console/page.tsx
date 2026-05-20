'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect, useState } from 'react';
import { API_BASE } from '@/lib/api';
import type { UserContext } from '@/lib/types';

const DEFAULT_TENANT = process.env.NEXT_PUBLIC_DEFAULT_TENANT || 'security';

/**
 * 관리자 콘솔 entry — /console.
 *
 * 산업 표준 (AWS console.aws.amazon.com, Azure portal 등) 패턴:
 *   - chat(user)과 admin(operator)은 별도 entry path. cookie는 same-origin이라
 *     공유되지만 진입 흐름·UI를 명확히 분리.
 *   - 다른 사람이 admin이면 incognito/다른 browser profile 권장.
 *
 * 동작:
 *   - 미인증: /security/chat 진입과 동일하게 SSO 시작 (path 자체가 protected
 *     하지 않으므로 본 페이지가 직접 처리)
 *   - PLATFORM_ADMIN: /platform/admin/tenants 로 자동 이동
 *   - ADMIN: 자기 tenant_id/admin/dashboard 로 자동 이동
 *   - USER: 권한 없음 화면 (chat으로 돌아가는 링크)
 */
export default function ConsoleEntry() {
  const router = useRouter();
  const [user, setUser] = useState<UserContext | null>(null);
  const [isLoading, setIsLoading] = useState(true);

  useEffect(() => {
    let cancelled = false;
    fetch(`${API_BASE}/api/auth/me`, { credentials: 'include' })
      .then(async (res) => {
        if (cancelled) return;
        if (res.status === 401) {
          // 미인증 — AWS console 패턴: 즉시 SSO redirect. tenant 미지정이므로
          // default tenant로 진행. 로그인 후 callback이 /security/chat 으로 데려가지만
          // 거기서 사용자가 다시 /console 입력 또는 다른 경로 필요. 산업 표준은
          // returnUrl을 IdP에 넘기지만 우리 SSO는 그걸 안 받으므로 일단 default tenant
          // SSO 시작만.
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
        // USER role은 아래 UI에서 안내
      })
      .catch(() => {})
      .finally(() => {
        if (!cancelled) setIsLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [router]);

  if (isLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center bg-gray-50 text-sm text-gray-500">
        권한 확인 중…
      </main>
    );
  }

  if (user && !user.is_admin && !user.is_platform_admin) {
    return (
      <main className="min-h-screen flex flex-col items-center justify-center bg-gray-50 p-8">
        <h1 className="text-xl font-semibold mb-2">관리자 권한 없음</h1>
        <p className="text-sm text-gray-600 mb-1">
          현재 계정: {user.preferred_username ?? user.email ?? user.user_id}
        </p>
        <p className="text-xs text-gray-500 mb-6">
          관리자 작업이 필요하면 다른 계정으로 별도 브라우저(시크릿 창 또는
          다른 프로필)에서 접속하세요.
        </p>
        <div className="flex gap-2">
          <Link
            href={`/${user.tenant_id}/chat`}
            className="px-4 py-2 bg-gray-900 text-white rounded-lg text-sm hover:bg-gray-800"
          >
            채팅으로 돌아가기
          </Link>
          <Link
            href="/"
            className="px-4 py-2 border border-gray-300 rounded-lg text-sm hover:bg-gray-100"
          >
            홈
          </Link>
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
