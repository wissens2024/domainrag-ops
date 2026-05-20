'use client';

import useSWR from 'swr';
import Card, { CardHeader } from '@/components/ui/Card';
import Badge from '@/components/ui/Badge';
import { account, swrFetcher } from '@/lib/api';
import type { UserContext } from '@/lib/types';

export default function ProfilePage() {
  const { data: user } = useSWR<UserContext>('/api/auth/me', swrFetcher, {
    shouldRetryOnError: false,
  });
  const { data: apps, error: appsError } = useSWR(
    user ? 'account.applications' : null,
    () => account.getApplications(),
  );

  if (!user) return null;

  const fields = [
    { label: '사용자 ID', value: user.user_id, mono: true },
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
          description="AuthFusion(SSO)에 등록된 본인 정보. 비밀번호·MFA는 보안 탭에서."
        />
        <dl className="grid grid-cols-1 sm:grid-cols-2 gap-x-8 gap-y-4">
          {fields.map((f) => (
            <div key={f.label}>
              <dt className="text-[11px] text-gray-500 font-medium uppercase tracking-wide">
                {f.label}
              </dt>
              <dd
                className={`mt-1 text-sm text-gray-900 ${
                  f.mono ? 'font-mono text-xs' : ''
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
        </div>
      </Card>

      <Card>
        <CardHeader
          title="연결된 애플리케이션"
          description="AuthFusion SSO로 본 계정으로 접근 가능한 RP."
        />
        {!apps && !appsError && (
          <p className="text-xs text-gray-400">로드 중…</p>
        )}
        {appsError && (
          <p className="text-xs text-red-600">
            로드 실패: {(appsError as Error).message}
          </p>
        )}
        {apps && apps.length === 0 && (
          <p className="text-xs text-gray-400">아직 연결된 애플리케이션이 없습니다.</p>
        )}
        {apps && apps.length > 0 && (
          <ul className="divide-y divide-gray-100">
            {apps.map((a) => (
              <li
                key={a.clientUuid}
                className="py-3 flex items-center justify-between gap-3"
              >
                <div className="min-w-0">
                  <div className="text-sm font-medium text-gray-900 flex items-center gap-2">
                    {a.clientName}
                    {!a.enabled && <Badge tone="neutral">비활성</Badge>}
                    {a.mfaRequired && <Badge tone="warn">MFA 필수</Badge>}
                  </div>
                  <div className="text-[11px] text-gray-500 font-mono mt-0.5 truncate">
                    {a.clientId}
                  </div>
                </div>
                <div className="flex flex-wrap gap-1 flex-shrink-0">
                  {a.roles.map((r) => (
                    <Badge key={r} tone="neutral">
                      {r}
                    </Badge>
                  ))}
                </div>
              </li>
            ))}
          </ul>
        )}
      </Card>

      {user.domain_groups.length > 0 && (
        <Card>
          <CardHeader
            title="도메인 그룹"
            description="ACL·검색 권한에 사용되는 group 멤버십 (DomainRAG)."
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
