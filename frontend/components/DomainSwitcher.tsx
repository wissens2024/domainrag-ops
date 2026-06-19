/**
 * DomainSwitcher — ADR-022 §7.
 *
 * 현재 활성 도메인을 항상 보이는 1급 요소로 노출하고, membership(또는 전역역할)으로
 * 접근 가능한 다른 도메인으로 재로그인 없이 전환한다. 멀티도메인 RAG에서 "어느
 * 도메인에 묻는가"는 답의 의미 자체이므로 숨기지 않는다.
 *
 * - 활성 도메인 = URL path의 domainId (prop).
 * - 목록 = GET /api/auth/me/domains (admin/auditor=전체, user=기본+배정).
 * - 1개뿐이면 칩만 표시(드롭다운 비활성), 여러 개면 전환 메뉴.
 * - 전환 시 같은 화면 종류(chat/admin/...)를 유지하며 도메인만 교체.
 */
'use client';

import { useRouter } from 'next/navigation';
import { useEffect, useRef, useState } from 'react';
import useSWR from 'swr';
import { getMyDomains, LAST_DOMAIN_KEY } from '@/lib/api';
import type { MyDomainsResult } from '@/lib/types';

interface Props {
  domainId: string;
  /** chat | admin 등 — 전환 시 유지할 화면 종류. 기본 chat. */
  section?: 'chat' | 'admin';
  /** 칩 톤. dark는 admin 사이드바(어두운 배경)용. */
  tone?: 'light' | 'dark';
}

export default function DomainSwitcher({
  domainId,
  section = 'chat',
  tone = 'light',
}: Props) {
  const router = useRouter();
  const [open, setOpen] = useState(false);
  const boxRef = useRef<HTMLDivElement>(null);

  const { data } = useSWR<MyDomainsResult>('me-domains', getMyDomains, {
    revalidateOnFocus: false,
  });

  // 마지막 사용 도메인 기억 (ADR-022 §7 착지 규칙 보조).
  useEffect(() => {
    if (domainId) {
      try {
        window.localStorage.setItem(LAST_DOMAIN_KEY, domainId);
      } catch {
        /* storage 차단 환경 무시 */
      }
    }
  }, [domainId]);

  // 바깥 클릭 시 닫기.
  useEffect(() => {
    if (!open) return;
    const onClick = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) {
        setOpen(false);
      }
    };
    document.addEventListener('mousedown', onClick);
    return () => document.removeEventListener('mousedown', onClick);
  }, [open]);

  const items = data?.items ?? [];
  const current = items.find((d) => d.domain_id === domainId);
  const label = current?.display_name || domainId;
  const canSwitch = items.length > 1;

  const go = (target: string) => {
    setOpen(false);
    if (target === domainId) return;
    // admin 섹션의 index는 /admin/dashboard (/admin 자체는 페이지 없음 → 404 회피).
    const sub = section === 'admin' ? 'admin/dashboard' : 'chat';
    router.push(`/${target}/${sub}`);
  };

  const chipClass =
    tone === 'dark'
      ? 'bg-gray-800 text-gray-100 hover:bg-gray-700 border-gray-700'
      : 'bg-white text-gray-900 hover:bg-gray-50 border-gray-200 dark:bg-slate-800 dark:text-slate-100 dark:hover:bg-slate-700 dark:border-slate-600';

  return (
    <div ref={boxRef} className="relative inline-block text-left">
      <button
        type="button"
        onClick={() => canSwitch && setOpen((v) => !v)}
        disabled={!canSwitch}
        className={`flex items-center gap-1.5 px-2.5 py-1 rounded-lg border text-xs font-medium transition-colors ${chipClass} ${
          canSwitch ? 'cursor-pointer' : 'cursor-default'
        }`}
        title={canSwitch ? '도메인 전환' : '현재 도메인'}
      >
        <span className="text-[10px] opacity-60">도메인</span>
        <span className="truncate max-w-[160px]">{label}</span>
        {canSwitch && <span className="opacity-60">▾</span>}
      </button>

      {open && canSwitch && (
        <div className="absolute left-0 mt-1 w-60 rounded-lg border border-gray-200 bg-white shadow-lg z-50 py-1 max-h-80 overflow-y-auto dark:border-slate-600 dark:bg-slate-800">
          {items.map((d) => {
            const active = d.domain_id === domainId;
            return (
              <button
                key={d.domain_id}
                onClick={() => go(d.domain_id)}
                className={`flex w-full items-center justify-between px-3 py-2 text-left text-sm hover:bg-gray-50 dark:hover:bg-slate-700 ${
                  active ? 'bg-gray-100 font-medium text-gray-900 dark:bg-slate-700 dark:text-slate-100' : 'text-gray-700 dark:text-slate-300'
                }`}
              >
                <span className="truncate">{d.display_name}</span>
                {active ? (
                  <span className="text-[10px] text-brand-600">현재</span>
                ) : (
                  <span className="text-[10px] text-gray-400">{d.domain_id}</span>
                )}
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
