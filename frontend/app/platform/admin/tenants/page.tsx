/**
 * Platform Tenants — /platform/admin/tenants (ADR-008 + ADR-017 §18).
 *
 * tenants 목록 + status 필터 + register / patch status / hard delete.
 */
'use client';

import Link from 'next/link';
import { useState } from 'react';
import useSWR, { mutate } from 'swr';
import Card from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import Badge from '@/components/ui/Badge';
import {
  hardDeleteTenant,
  listTenants,
  patchTenantStatus,
  registerTenant,
} from '@/lib/api';
import type { TenantListResult, TenantStatus } from '@/lib/types';

const STATUS_TONE: Record<TenantStatus, 'success' | 'warn' | 'neutral' | 'danger'> = {
  active: 'success',
  suspended: 'warn',
  archived: 'neutral',
  deleted: 'danger',
};

export default function PlatformTenantsPage() {
  const [statusFilter, setStatusFilter] = useState<TenantStatus | ''>('');
  const [showRegister, setShowRegister] = useState(false);
  const [form, setForm] = useState({
    domain_id: '',
    display_name: '',
    domain_type: 'security',
    embedding_model: 'bge-m3',
    modules: 'rag',
  });

  const swrKey = `platform-tenants:${statusFilter}`;
  const { data, isLoading } = useSWR<TenantListResult>(
    swrKey,
    () => listTenants({ status: statusFilter || undefined }),
  );

  const handleRegister = async (e: React.FormEvent) => {
    e.preventDefault();
    try {
      await registerTenant({
        domain_id: form.domain_id,
        display_name: form.display_name,
        domain_type: form.domain_type,
        embedding_model: form.embedding_model,
        modules: form.modules.split(',').map((s) => s.trim()),
      });
      setShowRegister(false);
      setForm({
        domain_id: '',
        display_name: '',
        domain_type: 'security',
        embedding_model: 'bge-m3',
        modules: 'rag',
      });
      void mutate(swrKey);
    } catch (e) {
      alert(`등록 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  const handlePatchStatus = async (tid: string, status: TenantStatus) => {
    const reason = prompt(`${tid} → ${status} 사유:`);
    if (reason === null) return;
    try {
      await patchTenantStatus(tid, status, reason || undefined);
      void mutate(swrKey);
    } catch (e) {
      alert(`전이 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  const handleHardDelete = async (tid: string) => {
    const reason = prompt(`${tid} 하드 삭제 사유 (필수, archived 상태에서만 가능):`);
    if (!reason) return;
    if (!confirm(`정말 ${tid}을 영구 삭제하시겠습니까? cross-system rollback 불가.`)) return;
    try {
      const result = await hardDeleteTenant(tid, reason);
      alert(
        `삭제 ${result.partial ? '부분 완료(일부 실패)' : '완료'}\nstatus: ${result.status}`,
      );
      void mutate(swrKey);
    } catch (e) {
      alert(`삭제 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6">
      <div className="flex justify-between mb-4">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-slate-100">Platform · Tenants</h1>
        <Button size="sm" onClick={() => setShowRegister(!showRegister)}>
          + 신규 등록
        </Button>
      </div>

      <div className="text-xs text-gray-500 dark:text-slate-400 mb-3">
        <Link href="/platform/admin/endpoints" className="text-blue-600 dark:text-brand-400 hover:underline">
          → Endpoints
        </Link>{' '}
        ·{' '}
        <Link href="/platform/admin/health" className="text-blue-600 dark:text-brand-400 hover:underline">
          Health metrics
        </Link>{' '}
        ·{' '}
        <Link
          href="/platform/admin/analytics/usage"
          className="text-blue-600 dark:text-brand-400 hover:underline"
        >
          Usage analytics
        </Link>
      </div>

      {showRegister && (
        <Card className="mb-4">
        <form
          onSubmit={handleRegister}
          className="grid grid-cols-5 gap-2 text-sm"
        >
          <input
            value={form.domain_id}
            onChange={(e) => setForm({ ...form, domain_id: e.target.value })}
            placeholder="domain_id (lowercase)"
            required
            className="px-2 py-1 border rounded"
          />
          <input
            value={form.display_name}
            onChange={(e) => setForm({ ...form, display_name: e.target.value })}
            placeholder="display_name"
            required
            className="px-2 py-1 border rounded"
          />
          <input
            value={form.domain_type}
            onChange={(e) => setForm({ ...form, domain_type: e.target.value })}
            placeholder="domain_type"
            className="px-2 py-1 border rounded"
          />
          <input
            value={form.embedding_model}
            onChange={(e) => setForm({ ...form, embedding_model: e.target.value })}
            placeholder="embedding_model"
            className="px-2 py-1 border rounded"
          />
          <input
            value={form.modules}
            onChange={(e) => setForm({ ...form, modules: e.target.value })}
            placeholder="modules (csv)"
            className="px-2 py-1 border rounded"
          />
          <Button type="submit" size="sm" className="col-span-5">
            등록 (Qdrant·MinIO·configs 자동 생성)
          </Button>
        </form>
        </Card>
      )}

      <div className="flex gap-2 mb-3 text-sm">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as TenantStatus | '')}
          className="px-3 py-1 border rounded"
        >
          <option value="">모든 상태</option>
          <option value="active">active</option>
          <option value="suspended">suspended</option>
          <option value="archived">archived</option>
          <option value="deleted">deleted</option>
        </select>
      </div>

      {isLoading && <p className="text-gray-500 dark:text-slate-400">로딩...</p>}
      {data && (
        <Card padded={false} className="overflow-hidden">
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="border-b border-gray-200 dark:border-slate-700 bg-gray-50 dark:bg-slate-900/50 text-left text-gray-500 dark:text-slate-400">
              <th className="p-2">domain_id</th>
              <th className="p-2">display_name</th>
              <th className="p-2">domain</th>
              <th className="p-2">embedding</th>
              <th className="p-2">status</th>
              <th className="p-2">생성</th>
              <th className="p-2">액션</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((t) => (
              <tr key={t.domain_id} className="border-b border-gray-100 dark:border-slate-700/60 hover:bg-gray-50 dark:hover:bg-slate-700/40">
                <td className="p-2 font-mono text-xs">{t.domain_id}</td>
                <td className="p-2 text-xs">{t.display_name}</td>
                <td className="p-2 text-xs">{t.domain_type}</td>
                <td className="p-2 text-xs">{t.embedding_model}</td>
                <td className="p-2">
                  <Badge tone={STATUS_TONE[t.status]}>{t.status}</Badge>
                </td>
                <td className="p-2 text-xs">
                  {new Date(t.created_at).toLocaleDateString('ko-KR')}
                </td>
                <td className="p-2 text-xs space-x-1 whitespace-nowrap">
                  {t.status === 'active' && (
                    <Button variant="secondary" size="sm" onClick={() => void handlePatchStatus(t.domain_id, 'suspended')}>
                      suspend
                    </Button>
                  )}
                  {t.status === 'suspended' && (
                    <Button variant="secondary" size="sm" onClick={() => void handlePatchStatus(t.domain_id, 'active')}>
                      reactivate
                    </Button>
                  )}
                  {(t.status === 'active' || t.status === 'suspended') && (
                    <Button variant="secondary" size="sm" onClick={() => void handlePatchStatus(t.domain_id, 'archived')}>
                      archive
                    </Button>
                  )}
                  {t.status === 'archived' && (
                    <>
                      <Button variant="secondary" size="sm" onClick={() => void handlePatchStatus(t.domain_id, 'active')}>
                        restore
                      </Button>
                      <Button variant="danger" size="sm" onClick={() => void handleHardDelete(t.domain_id)}>
                        hard delete
                      </Button>
                    </>
                  )}
                </td>
              </tr>
            ))}
            {data.items.length === 0 && (
              <tr>
                <td colSpan={7} className="p-4 text-center text-gray-500 dark:text-slate-400">
                  tenants가 없습니다.
                </td>
              </tr>
            )}
          </tbody>
        </table>
        </Card>
      )}
    </div>
  );
}
