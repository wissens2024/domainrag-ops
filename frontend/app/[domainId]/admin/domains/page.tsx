/**
 * 도메인 관리 — `/{domainId}/admin/domains` (ADR-022 §3·§4).
 *
 * admin/platform_admin 전역. 도메인 목록 + 선택 도메인의 enrollment_policy 전환 +
 * 배정된 사용자(membership) 목록 + 사용자 배정/해제.
 *
 * 사용자 배정은 AuthFusion sub(user_id)로 한다. IdP 사용자 디렉터리 검색(email→sub)은
 * AuthFusion admin API 결선이 필요해 후속 작업(현재는 sub 직접 입력).
 *
 * 디자인 시스템(ui/) 적용 (ADR-016 보강).
 */
'use client';

import { useEffect, useState } from 'react';
import useSWR, { mutate } from 'swr';
import Card from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import Badge from '@/components/ui/Badge';
import {
  assignDomainMember,
  getMyDomains,
  listDomainMembers,
  revokeDomainMember,
  setDomainEnrollmentPolicy,
} from '@/lib/api';
import type { AccessibleDomain, DomainMember, MyDomainsResult } from '@/lib/types';

export default function DomainManagementPage() {
  const { data: domains } = useSWR<MyDomainsResult>('me-domains', getMyDomains);
  const [selected, setSelected] = useState<string | null>(null);

  const items = domains?.items ?? [];
  useEffect(() => {
    if (!selected && items.length > 0) setSelected(items[0].domain_id);
  }, [items, selected]);

  return (
    <div className="p-8 max-w-6xl mx-auto font-sans">
      <div className="mb-6">
        <h1 className="text-2xl font-semibold text-gray-900 dark:text-slate-100 tracking-tight">
          도메인 관리
        </h1>
        <p className="text-sm text-gray-500 dark:text-slate-400 mt-1">
          도메인 가입 정책 전환 + 사용자 배정 (admin 전역, ADR-022)
        </p>
      </div>

      {!domains && <div className="text-sm text-gray-500 dark:text-slate-400">로딩 중…</div>}

      {domains && (
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          <div className="md:col-span-1">
            <Card padded={false} className="overflow-hidden">
              {items.map((d) => (
                <DomainRow
                  key={d.domain_id}
                  domain={d}
                  active={d.domain_id === selected}
                  onClick={() => setSelected(d.domain_id)}
                />
              ))}
              {items.length === 0 && (
                <p className="p-4 text-sm text-gray-400 dark:text-slate-500">도메인이 없습니다.</p>
              )}
            </Card>
          </div>

          <div className="md:col-span-2">
            {selected ? (
              <DomainDetail
                domainId={selected}
                domain={items.find((d) => d.domain_id === selected)}
              />
            ) : (
              <p className="text-sm text-gray-400 dark:text-slate-500">도메인을 선택하세요.</p>
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function DomainRow({
  domain,
  active,
  onClick,
}: {
  domain: AccessibleDomain;
  active: boolean;
  onClick: () => void;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex w-full items-center justify-between px-4 py-3 text-left border-b border-gray-100 dark:border-slate-700/60 last:border-0 transition-colors ${
        active
          ? 'bg-gray-900 text-white dark:bg-brand-600'
          : 'hover:bg-gray-50 text-gray-800 dark:text-slate-200 dark:hover:bg-slate-700/60'
      }`}
    >
      <div className="min-w-0">
        <p className="text-sm font-medium truncate">{domain.display_name}</p>
        <p
          className={`text-[11px] truncate ${
            active ? 'text-gray-300 dark:text-brand-100' : 'text-gray-400 dark:text-slate-500'
          }`}
        >
          {domain.domain_id}
        </p>
      </div>
      <Badge tone={domain.enrollment_policy === 'open' ? 'success' : 'neutral'}>
        {domain.enrollment_policy === 'open' ? '개방' : '배정'}
      </Badge>
    </button>
  );
}

function DomainDetail({
  domainId,
  domain,
}: {
  domainId: string;
  domain?: AccessibleDomain;
}) {
  const { data } = useSWR<{ domain_id: string; members: DomainMember[] }>(
    `domain-members:${domainId}`,
    () => listDomainMembers(domainId),
  );
  const [newUserId, setNewUserId] = useState('');
  const [clearance, setClearance] = useState('internal');
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const refresh = () => mutate(`domain-members:${domainId}`);

  const onAssign = async () => {
    if (!newUserId.trim()) return;
    setBusy(true);
    setErr(null);
    try {
      await assignDomainMember(domainId, {
        user_id: newUserId.trim(),
        clearance,
      });
      setNewUserId('');
      refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : '배정 실패');
    } finally {
      setBusy(false);
    }
  };

  const onRevoke = async (userId: string) => {
    setBusy(true);
    setErr(null);
    try {
      await revokeDomainMember(domainId, userId);
      refresh();
    } catch (e) {
      setErr(e instanceof Error ? e.message : '해제 실패');
    } finally {
      setBusy(false);
    }
  };

  const togglePolicy = async () => {
    if (!domain) return;
    const next = domain.enrollment_policy === 'open' ? 'assigned' : 'open';
    setBusy(true);
    setErr(null);
    try {
      await setDomainEnrollmentPolicy(domainId, next);
      mutate('me-domains');
    } catch (e) {
      setErr(e instanceof Error ? e.message : '정책 변경 실패');
    } finally {
      setBusy(false);
    }
  };

  const members = (data?.members ?? []).filter((m) => m.is_active);

  return (
    <div className="space-y-5">
      <Card>
        <div className="flex items-center justify-between">
          <div>
            <h2 className="text-sm font-semibold text-gray-900 dark:text-slate-100">
              {domain?.display_name ?? domainId}
            </h2>
            <p className="text-[11px] text-gray-500 dark:text-slate-400 mt-0.5">
              가입 정책:{' '}
              <span className="font-medium text-gray-700 dark:text-slate-200">
                {domain?.enrollment_policy === 'open'
                  ? '개방 (모든 사용자 자동 접근)'
                  : '배정 (관리자 배정만)'}
              </span>
            </p>
          </div>
          <Button variant="secondary" size="sm" onClick={togglePolicy} disabled={busy || !domain}>
            {domain?.enrollment_policy === 'open' ? '배정으로 전환' : '개방으로 전환'}
          </Button>
        </div>
      </Card>

      <Card>
        <h3 className="text-sm font-semibold text-gray-900 dark:text-slate-100 mb-3">사용자 배정</h3>
        <div className="flex flex-wrap gap-2 items-center">
          <input
            value={newUserId}
            onChange={(e) => setNewUserId(e.target.value)}
            placeholder="사용자 ID (AuthFusion sub)"
            className="flex-1 min-w-[220px] px-3 py-2 border border-gray-300 rounded-lg text-sm"
          />
          <select
            value={clearance}
            onChange={(e) => setClearance(e.target.value)}
            className="px-3 py-2 border border-gray-300 rounded-lg text-sm"
          >
            <option value="public">public</option>
            <option value="internal">internal</option>
            <option value="confidential">confidential</option>
            <option value="secret">secret</option>
          </select>
          <Button variant="primary" onClick={onAssign} disabled={busy || !newUserId.trim()}>
            배정
          </Button>
        </div>
        {err && <p className="text-xs text-red-600 dark:text-red-400 mt-2">{err}</p>}
        <p className="text-[11px] text-gray-400 dark:text-slate-500 mt-2">
          이메일→ID 검색은 AuthFusion 디렉터리 연동 후 제공됩니다. 현재는 sub 직접 입력.
        </p>
      </Card>

      <Card>
        <h3 className="text-sm font-semibold text-gray-900 dark:text-slate-100 mb-3">
          배정된 사용자 ({members.length})
        </h3>
        {!data && <p className="text-sm text-gray-400 dark:text-slate-500">로딩 중…</p>}
        {data && members.length === 0 && (
          <p className="text-sm text-gray-400 dark:text-slate-500">배정된 사용자가 없습니다.</p>
        )}
        <div className="divide-y divide-gray-100 dark:divide-slate-700/60">
          {members.map((m) => (
            <div key={m.user_id} className="flex items-center justify-between py-2">
              <div className="min-w-0">
                <p className="text-sm text-gray-900 dark:text-slate-100 truncate">{m.user_id}</p>
                <p className="text-[11px] text-gray-500 dark:text-slate-400">
                  clearance: {m.clearance}
                  {m.department ? ` · ${m.department}` : ''}
                </p>
              </div>
              <Button variant="ghost" size="sm" onClick={() => onRevoke(m.user_id)} disabled={busy}>
                해제
              </Button>
            </div>
          ))}
        </div>
      </Card>
    </div>
  );
}
