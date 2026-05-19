/**
 * AdminLayout — ADR-016 §3 + Y9.
 * /{tenantId}/admin/* 모든 admin 페이지의 공통 sidebar + 헤더.
 *
 * RBAC: USER가 직접 admin route에 접근하면 backend가 403 — 화면도 친절히 안내.
 */
'use client';

import { useParams } from 'next/navigation';
import AdminSidebar from '@/components/AdminSidebar';

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const params = useParams<{ tenantId: string }>();
  const tenantId = params.tenantId;

  return (
    <div className="flex h-screen">
      <AdminSidebar tenantId={tenantId} />
      <main className="flex-1 overflow-y-auto bg-white">{children}</main>
    </div>
  );
}
