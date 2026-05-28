/**
 * AdminLayout — /{domainId}/admin/* 공통 chrome.
 * RBAC·사이드바·상단바는 AdminAppShell이 담당 (ADR-016 보강, WiSentinel 골격).
 */
'use client';

import { useParams } from 'next/navigation';
import AdminAppShell from '@/components/AdminAppShell';

export default function AdminLayout({ children }: { children: React.ReactNode }) {
  const params = useParams<{ domainId: string }>();
  return <AdminAppShell domainId={params.domainId}>{children}</AdminAppShell>;
}
