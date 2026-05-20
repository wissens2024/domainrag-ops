'use client';

import useSWR from 'swr';
import Card, { CardHeader } from '@/components/ui/Card';
import Badge from '@/components/ui/Badge';
import { swrFetcher } from '@/lib/api';
import type { UserContext } from '@/lib/types';

export default function ProfilePage() {
  const { data: user } = useSWR<UserContext>('/api/auth/me', swrFetcher, {
    shouldRetryOnError: false,
  });

  if (!user) return null;

  const fields: Array<{ label: string; value: string | null | undefined; tone?: 'mono' }> = [
    { label: '사용자 ID', value: user.user_id, tone: 'mono' },
    { label: '이름', value: user.preferred_username },
    { label: '이메일', value: user.email },
    { label: '소속 tenant', value: user.tenant_id },
    { label: '부서', value: user.department },
    { label: '보안 등급', value: user.clearance },
  ];

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          title="기본 정보"
          description="AuthFusion에 등록된 본인 정보. 변경은 보안 탭에서."
        />
        <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-4">
          {fields.map((f) => (
            <div key={f.label}>
              <dt className="text-[11px] text-gray-500 font-medium uppercase tracking-wide">
                {f.label}
              </dt>
              <dd
                className={`mt-1 text-sm text-gray-900 ${
                  f.tone === 'mono' ? 'font-mono text-xs' : ''
                }`}
              >
                {f.value || <span className="text-gray-400">—</span>}
              </dd>
            </div>
          ))}
        </dl>
      </Card>

      <Card>
        <CardHeader title="권한" description="현재 계정에 부여된 role." />
        <div className="flex flex-wrap gap-1.5">
          {user.roles.length > 0 ? (
            user.roles.map((r) => (
              <Badge key={r} tone={r.includes('ADMIN') ? 'brand' : 'neutral'}>
                {r}
              </Badge>
            ))
          ) : (
            <span className="text-xs text-gray-400">권한 없음</span>
          )}
          {user.is_platform_admin && <Badge tone="brand">PLATFORM_ADMIN</Badge>}
          {user.is_admin && !user.roles.includes('ADMIN') && (
            <Badge tone="brand">ADMIN</Badge>
          )}
        </div>
      </Card>

      {user.domain_groups.length > 0 && (
        <Card>
          <CardHeader
            title="도메인 그룹"
            description="ACL 검색·접근 제어에 사용되는 group 멤버십."
          />
          <div className="flex flex-wrap gap-1.5">
            {user.domain_groups.map((g) => (
              <Badge key={g} tone="info">
                {g}
              </Badge>
            ))}
          </div>
        </Card>
      )}
    </div>
  );
}
