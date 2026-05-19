'use client';

import Link from 'next/link';
import { useRouter } from 'next/navigation';
import { useEffect } from 'react';
import useSWR from 'swr';
import { swrFetcher } from '@/lib/api';
import type { UserContext } from '@/lib/types';

/**
 * 랜딩 페이지 (ADR-018).
 *
 * 동작:
 *   - SSR 통과 후 client에서 /api/auth/me 조회
 *   - 인증되었으면 자기 tenant chat(/{tenant_id}/chat)으로 즉시 redirect
 *   - 미인증 (401) — SWR error → 랜딩 본문 노출. 단 admin/platform 링크는
 *     role 검증 후만 표시 (RBAC 메뉴 필터링, ADR-016 Y9).
 *
 * dev 모드에서 SSO endpoint가 없거나 cookie 없는 경우 default tenant 링크로
 * 폴백 표시 (NEXT_PUBLIC_DEFAULT_TENANT).
 */
export default function Home() {
  const router = useRouter();
  const defaultTenant = process.env.NEXT_PUBLIC_DEFAULT_TENANT || 'security';

  const { data: user, error, isLoading } = useSWR<UserContext>(
    '/api/auth/me',
    swrFetcher,
    { shouldRetryOnError: false },
  );

  useEffect(() => {
    if (user) {
      router.replace(`/${user.tenant_id}/chat`);
    }
  }, [user, router]);

  if (isLoading) {
    return (
      <main className="min-h-screen flex items-center justify-center p-8 text-sm text-gray-500">
        로그인 확인 중…
      </main>
    );
  }
  if (user) {
    return (
      <main className="min-h-screen flex items-center justify-center p-8 text-sm text-gray-500">
        {user.tenant_id} 채팅으로 이동 중…
      </main>
    );
  }

  // 미인증 — 랜딩. role 모름이라 admin/platform 링크는 노출 안 함.
  void error;
  return (
    <main className="min-h-screen flex flex-col items-center justify-center p-8">
      <h1 className="text-3xl font-bold mb-2">DomainRAG Ops</h1>
      <p className="text-gray-600 mb-8">폐쇄망 멀티테넌트 RAG 플랫폼</p>

      <div className="space-y-2 w-64">
        <Link
          href={`/${defaultTenant}/chat`}
          className="block px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 text-center"
        >
          로그인 후 채팅 시작
        </Link>
      </div>

      <p className="text-xs text-gray-500 mt-8">
        링크 클릭 시 SSO 로그인 화면으로 이동합니다.
      </p>
    </main>
  );
}
