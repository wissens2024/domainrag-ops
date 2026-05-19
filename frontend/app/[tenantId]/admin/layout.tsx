/**
 * AdminLayout — ADR-016 §3 + Y9.
 * /{tenantId}/admin/* 모든 admin 페이지의 공통 sidebar + 헤더.
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
  const params = useParams<{ tenantId: string }>();
  const router = useRouter();
  const tenantId = params.tenantId;

  const { data: user, isLoading } = useSWR<UserContext>(
    '/api/auth/me',
    swrFetcher,
  );

  useEffect(() => {
    if (!isLoading && user && !user.is_admin && !user.is_platform_admin) {
      router.replace(`/${tenantId}/chat`);
    }
  }, [user, isLoading, router, tenantId]);

  if (isLoading || !user) {
    return <div className="p-6 text-sm text-gray-500">권한 확인 중…</div>;
  }
  if (!user.is_admin && !user.is_platform_admin) {
    return <div className="p-6 text-sm text-gray-500">권한 없음 — 이동 중…</div>;
  }

  return (
    <div className="flex h-screen">
      <AdminSidebar tenantId={tenantId} />
      <main className="flex-1 overflow-y-auto bg-white">{children}</main>
    </div>
  );
}
