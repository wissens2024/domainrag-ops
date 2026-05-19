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
import { getCurrentUser, swrFetcher } from '@/lib/api';
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

function tenantMenuGroups(tenantId: string): MenuGroup[] {
  const base = `/${tenantId}/admin`;
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
      items: [{ label: 'Tenant Configs', href: `${base}/configs` }],
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
  tenantId: string;
}

export default function AdminSidebar({ tenantId }: Props) {
  const pathname = usePathname();
  const [showPlatform, setShowPlatform] = useState(false);

  // 현재 user 정보 (RBAC menu visibility용)
  const { data: user } = useSWR<UserContext>(
    tenantId ? `/api/${tenantId}/me` : null,
    swrFetcher,
  );

  const isPlatformAdmin = user?.roles.includes('PLATFORM_ADMIN');

  const groups = showPlatform && isPlatformAdmin
    ? platformMenuGroups()
    : tenantMenuGroups(tenantId);

  return (
    <aside className="w-64 border-r border-gray-200 bg-gray-50 flex flex-col">
      <div className="p-4 border-b border-gray-200">
        <div className="flex items-center justify-between">
          <p className="font-bold text-sm">
            {showPlatform ? '🌐 Platform' : `🏢 ${tenantId}`}
          </p>
          {isPlatformAdmin && (
            <button
              onClick={() => setShowPlatform(!showPlatform)}
              className="text-xs text-blue-600 hover:underline"
            >
              {showPlatform ? `← ${tenantId}` : 'Platform ▾'}
            </button>
          )}
        </div>
        {user && (
          <p className="text-xs text-gray-500 mt-1">
            {user.preferred_username || user.user_id} ·{' '}
            {user.roles.join(',')}
          </p>
        )}
      </div>

      <nav className="flex-1 overflow-y-auto py-2">
        {groups.map((g) => (
          <div key={g.label} className="mb-2">
            {g.label && (
              <p className="px-3 py-1 text-xs font-bold text-gray-500 uppercase">
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
                  className={`block px-4 py-1.5 text-sm ${
                    active
                      ? 'bg-blue-100 text-blue-900 border-l-4 border-blue-600'
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

      <div className="p-3 border-t border-gray-200">
        <Link
          href={`/${tenantId}/chat`}
          className="block text-xs text-blue-600 hover:underline"
        >
          ← 채팅으로 돌아가기
        </Link>
      </div>
    </aside>
  );
}
