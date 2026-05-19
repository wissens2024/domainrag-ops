/**
 * Platform Tenants — /platform/admin/tenants (ADR-008 + ADR-017 §18).
 *
 * tenants 목록 + status 필터 + register / patch status / hard delete.
 */
'use client';

import Link from 'next/link';
import { useState } from 'react';
import useSWR, { mutate } from 'swr';
import {
  hardDeleteTenant,
  listTenants,
  patchTenantStatus,
  registerTenant,
} from '@/lib/api';
import type { TenantListResult, TenantStatus } from '@/lib/types';

const STATUS_COLOR: Record<TenantStatus, string> = {
  active: 'bg-green-100 text-green-700',
  suspended: 'bg-yellow-100 text-yellow-700',
  archived: 'bg-gray-200 text-gray-700',
  deleted: 'bg-red-100 text-red-700',
};

export default function PlatformTenantsPage() {
  const [statusFilter, setStatusFilter] = useState<TenantStatus | ''>('');
  const [showRegister, setShowRegister] = useState(false);
  const [form, setForm] = useState({
    tenant_id: '',
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
        tenant_id: form.tenant_id,
        display_name: form.display_name,
        domain_type: form.domain_type,
        embedding_model: form.embedding_model,
        modules: form.modules.split(',').map((s) => s.trim()),
      });
      setShowRegister(false);
      setForm({
        tenant_id: '',
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
        <h1 className="text-2xl font-bold">Platform · Tenants</h1>
        <button
          onClick={() => setShowRegister(!showRegister)}
          className="px-3 py-2 bg-blue-600 text-white rounded text-sm"
        >
          + 신규 등록
        </button>
      </div>

      <div className="text-xs text-gray-500 mb-3">
        <Link href="/platform/admin/endpoints" className="text-blue-600 hover:underline">
          → Endpoints
        </Link>{' '}
        ·{' '}
        <Link href="/platform/admin/health" className="text-blue-600 hover:underline">
          Health metrics
        </Link>{' '}
        ·{' '}
        <Link
          href="/platform/admin/analytics/usage"
          className="text-blue-600 hover:underline"
        >
          Usage analytics
        </Link>
      </div>

      {showRegister && (
        <form
          onSubmit={handleRegister}
          className="border rounded p-4 mb-4 grid grid-cols-5 gap-2 text-sm"
        >
          <input
            value={form.tenant_id}
            onChange={(e) => setForm({ ...form, tenant_id: e.target.value })}
            placeholder="tenant_id (lowercase)"
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
          <button
            type="submit"
            className="col-span-5 px-3 py-1 bg-green-600 text-white rounded"
          >
            등록 (Qdrant·MinIO·configs 자동 생성)
          </button>
        </form>
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

      {isLoading && <p>로딩...</p>}
      {data && (
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="border-b bg-gray-50 text-left">
              <th className="p-2">tenant_id</th>
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
              <tr key={t.tenant_id} className="border-b hover:bg-gray-50">
                <td className="p-2 font-mono text-xs">{t.tenant_id}</td>
                <td className="p-2 text-xs">{t.display_name}</td>
                <td className="p-2 text-xs">{t.domain_type}</td>
                <td className="p-2 text-xs">{t.embedding_model}</td>
                <td className="p-2">
                  <span className={`px-2 py-0.5 rounded text-xs ${STATUS_COLOR[t.status]}`}>
                    {t.status}
                  </span>
                </td>
                <td className="p-2 text-xs">
                  {new Date(t.created_at).toLocaleDateString('ko-KR')}
                </td>
                <td className="p-2 text-xs space-x-1">
                  {t.status === 'active' && (
                    <button
                      onClick={() => void handlePatchStatus(t.tenant_id, 'suspended')}
                      className="px-1 border rounded"
                    >
                      suspend
                    </button>
                  )}
                  {t.status === 'suspended' && (
                    <button
                      onClick={() => void handlePatchStatus(t.tenant_id, 'active')}
                      className="px-1 border rounded"
                    >
                      reactivate
                    </button>
                  )}
                  {(t.status === 'active' || t.status === 'suspended') && (
                    <button
                      onClick={() => void handlePatchStatus(t.tenant_id, 'archived')}
                      className="px-1 border rounded"
                    >
                      archive
                    </button>
                  )}
                  {t.status === 'archived' && (
                    <>
                      <button
                        onClick={() => void handlePatchStatus(t.tenant_id, 'active')}
                        className="px-1 border rounded"
                      >
                        restore
                      </button>
                      <button
                        onClick={() => void handleHardDelete(t.tenant_id)}
                        className="px-1 border rounded text-red-600"
                      >
                        hard delete
                      </button>
                    </>
                  )}
                </td>
              </tr>
            ))}
            {data.items.length === 0 && (
              <tr>
                <td colSpan={7} className="p-4 text-center text-gray-500">
                  tenants가 없습니다.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
