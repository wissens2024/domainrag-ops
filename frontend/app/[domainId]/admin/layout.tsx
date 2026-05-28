/**
 * AdminLayout — ADR-016 §3 + Y9.
 * /{domainId}/admin/* 모든 admin 페이지의 공통 sidebar + 헤더.
 *
 * RBAC: client-side에서 user.is_admin 확인 후 미달이면 자기 tenant chat으로
 * redirect. backend는 별도 endpoint에서 다시 403으로 막는다 (이중 방어).
 */
'use client';

import { useParams, useRouter } from 'next/navigation';
import { useEffect } from 'react';
import useSWR from 'swr';
import AdminSidebar from '@/components/AdminSidebar';
import { swrFetcher } from '@/lib/api';
import type { UserContext } from '@/lib/types';

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const params = useParams<{ domainId: string }>();
  const router = useRouter();
  const domainId = params.domainId;

  const { data: user, isLoading } = useSWR<UserContext>(
    '/api/auth/me',
    swrFetcher,
  );

  // ADR-022 §3 — admin/platform_admin은 read-write, auditor는 read-only 전역 접근.
  const canAccessConsole =
    !!user && (user.is_admin || user.is_platform_admin || user.is_auditor);

  useEffect(() => {
    if (!isLoading && user && !canAccessConsole) {
      router.replace(`/${domainId}/chat`);
    }
  }, [user, isLoading, router, domainId, canAccessConsole]);

  if (isLoading || !user) {
    return <div className="p-6 text-sm text-gray-500">권한 확인 중…</div>;
  }
  if (!canAccessConsole) {
    return <div className="p-6 text-sm text-gray-500">권한 없음 — 이동 중…</div>;
  }

  return (
    <div className="flex h-screen bg-gray-50">
      <AdminSidebar domainId={domainId} />
      <main className="flex-1 overflow-y-auto">{children}</main>
    </div>
  );
}
