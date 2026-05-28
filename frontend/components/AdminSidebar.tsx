/**
 * AdminSidebar — ADR-016 §3 + Y9 RBAC 메뉴 매핑.
 *
 * USER는 admin 메뉴 자체에 접근 불가 (middleware/RBAC에서 차단).
 * ADMIN: tenant scope 메뉴 모두.
 * PLATFORM_ADMIN: + /platform/admin/* 메뉴 토글.
 */
'use client';

import Link from 'next/link';
import { usePathname } from 'next/navigation';
import { useEffect, useState } from 'react';
import useSWR from 'swr';
import DomainSwitcher from '@/components/DomainSwitcher';
import { getCurrentUser, logout, swrFetcher } from '@/lib/api';
import type { UserContext } from '@/lib/types';

interface MenuItem {
  label: string;
  href: string;
  exact?: boolean;
}

interface MenuGroup {
  label: string;
  items: MenuItem[];
}

function tenantMenuGroups(domainId: string): MenuGroup[] {
  const base = `/${domainId}/admin`;
  return [
    {
      label: '',
      items: [{ label: '대시보드', href: `${base}/dashboard` }],
    },
    {
      label: '지식 운영',
      items: [
        { label: '문서 관리', href: `${base}/documents` },
        { label: '인덱싱 모니터링', href: `${base}/indexing` },
        { label: 'Schema Editor', href: `${base}/schema` },
      ],
    },
    {
      label: '질의 운영',
      items: [
        { label: 'Chat Logs', href: `${base}/logs/chat` },
        { label: 'Citation Inspector', href: `${base}/citation-inspector` },
      ],
    },
    {
      label: '모델·라우팅',
      items: [
        { label: 'Routing Rules', href: `${base}/routing` },
        { label: 'Prompt Studio', href: `${base}/prompts` },
        { label: 'LoRA Registry', href: `${base}/lora` },
      ],
    },
    {
      label: '평가',
      items: [{ label: 'Evaluation Console', href: `${base}/evaluation` }],
    },
    {
      label: 'Assessment',
      items: [
        { label: 'Item Bank', href: `${base}/assessment/items` },
        { label: 'Generation Workbench', href: `${base}/assessment/workbench` },
        { label: 'Review Queue', href: `${base}/assessment/review-queue` },
      ],
    },
    {
      label: '설정',
      items: [
        { label: '도메인 관리', href: `${base}/domains` },
        { label: 'Tenant Configs', href: `${base}/configs` },
      ],
    },
  ];
}

function platformMenuGroups(): MenuGroup[] {
  const base = '/platform/admin';
  return [
    {
      label: 'Platform',
      items: [
        { label: 'Tenants', href: `${base}/tenants` },
        { label: 'Endpoints', href: `${base}/endpoints` },
        { label: 'Usage Analytics', href: `${base}/analytics/usage` },
        { label: 'Health Metrics', href: `${base}/health` },
        { label: 'Platform Configs', href: `${base}/configs` },
      ],
    },
  ];
}

interface Props {
  domainId: string;
}

export default function AdminSidebar({ domainId }: Props) {
  const pathname = usePathname();
  const [showPlatform, setShowPlatform] = useState(false);

  // 현재 user 정보 (RBAC menu visibility용) — cross-tenant /api/auth/me
  const { data: user } = useSWR<UserContext>('/api/auth/me', swrFetcher);

  const isPlatformAdmin = user?.is_platform_admin ?? false;

  const groups = showPlatform && isPlatformAdmin
    ? platformMenuGroups()
    : tenantMenuGroups(domainId);

  return (
    <aside className="w-64 border-r border-gray-200 bg-white flex flex-col font-sans">
      <div className="px-4 py-3.5 border-b border-gray-200">
        <div className="flex items-center justify-between gap-2">
          <div className="flex items-center gap-2 min-w-0">
            <div className={`w-7 h-7 rounded-lg flex items-center justify-center text-xs font-bold ${showPlatform ? 'bg-brand-600 text-white' : 'bg-gray-900 text-white'}`}>
              {showPlatform ? '🌐' : domainId.charAt(0).toUpperCase()}
            </div>
            <p className="font-semibold text-sm text-gray-900 truncate">
              {showPlatform ? 'Platform' : domainId}
            </p>
          </div>
          {isPlatformAdmin && (
            <button
              onClick={() => setShowPlatform(!showPlatform)}
              className="text-[10px] text-gray-500 hover:text-gray-900 px-1.5 py-0.5 hover:bg-gray-100 rounded"
            >
              {showPlatform ? '← tenant' : 'Platform ▾'}
            </button>
          )}
        </div>
        {user && (
          <p className="text-[10px] text-gray-500 mt-2 truncate flex items-center gap-1">
            <span className="text-gray-700 font-medium truncate">
              {user.preferred_username || user.user_id}
            </span>
            <span className="text-gray-300">·</span>
            <span className="truncate">{user.roles.join(',')}</span>
          </p>
        )}
        {!showPlatform && (
          <div className="mt-2">
            <DomainSwitcher domainId={domainId} section="admin" />
          </div>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto py-2">
        {groups.map((g) => (
          <div key={g.label} className="mb-3">
            {g.label && (
              <p className="px-3 py-1 text-[10px] font-semibold text-gray-400 uppercase tracking-wider">
                {g.label}
              </p>
            )}
            {g.items.map((item) => {
              const active = item.exact
                ? pathname === item.href
                : pathname?.startsWith(item.href);
              return (
                <Link
                  key={item.href}
                  href={item.href}
                  className={`flex items-center px-3 py-1.5 mx-1.5 my-0.5 rounded-md text-sm transition-colors ${
                    active
                      ? 'bg-gray-900 text-white font-medium'
                      : 'text-gray-700 hover:bg-gray-100'
                  }`}
                >
                  {item.label}
                </Link>
              );
            })}
          </div>
        ))}
      </nav>

      <div className="p-2 border-t border-gray-200 space-y-0.5">
        <Link
          href={`/${domainId}/chat`}
          className="block w-full px-3 py-1.5 rounded-md text-xs text-gray-700 hover:bg-gray-100 transition-colors"
        >
          ← 채팅으로 돌아가기
        </Link>
        <Link
          href="/account"
          className="block w-full px-3 py-1.5 rounded-md text-xs text-gray-700 hover:bg-gray-100 transition-colors"
        >
          내 계정
        </Link>
        {user && (
          <button
            onClick={async () => {
              const url = await logout(domainId);
              window.location.href = url || '/';
            }}
            className="block w-full text-left px-3 py-1.5 rounded-md text-xs text-gray-500 hover:bg-gray-100 hover:text-gray-700"
          >
            로그아웃
          </button>
        )}
      </div>
    </aside>
  );
}
