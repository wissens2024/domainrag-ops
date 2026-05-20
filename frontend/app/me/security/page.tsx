'use client';

import { useState } from 'react';
import useSWR from 'swr';
import Card, { CardHeader } from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import Input from '@/components/ui/Input';
import Badge from '@/components/ui/Badge';
import { swrFetcher } from '@/lib/api';
import type { UserContext } from '@/lib/types';

export default function SecurityPage() {
  const { data: user } = useSWR<UserContext>('/api/auth/me', swrFetcher, {
    shouldRetryOnError: false,
  });

  // Password change state
  const [current, setCurrent] = useState('');
  const [next1, setNext1] = useState('');
  const [next2, setNext2] = useState('');
  const [pwLoading, setPwLoading] = useState(false);
  const [pwMessage, setPwMessage] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const handleChangePassword = async (e: React.FormEvent) => {
    e.preventDefault();
    setPwMessage(null);
    if (next1.length < 8) {
      setPwMessage({ kind: 'err', text: '새 비밀번호는 8자 이상이어야 합니다.' });
      return;
    }
    if (next1 !== next2) {
      setPwMessage({ kind: 'err', text: '새 비밀번호가 일치하지 않습니다.' });
      return;
    }
    setPwLoading(true);
    try {
      const res = await fetch('/api/auth/me/change-password', {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ current_password: current, new_password: next1 }),
      });
      if (!res.ok) {
        const body = await res.json().catch(() => ({}));
        throw new Error(body?.detail?.error || `HTTP ${res.status}`);
      }
      setPwMessage({ kind: 'ok', text: '비밀번호가 변경되었습니다.' });
      setCurrent('');
      setNext1('');
      setNext2('');
    } catch (err) {
      setPwMessage({
        kind: 'err',
        text: err instanceof Error ? err.message : '변경 실패',
      });
    } finally {
      setPwLoading(false);
    }
  };

  if (!user) return null;

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          title="비밀번호"
          description="AuthFusion(SSO) 계정 비밀번호. 변경 시 다른 device의 세션은 유지됩니다 — 모두 종료하려면 세션 탭."
        />
        <form onSubmit={handleChangePassword} className="space-y-4 max-w-md">
          <Input
            type="password"
            name="current_password"
            label="현재 비밀번호"
            value={current}
            onChange={(e) => setCurrent(e.target.value)}
            required
            autoComplete="current-password"
          />
          <Input
            type="password"
            name="new_password"
            label="새 비밀번호"
            value={next1}
            onChange={(e) => setNext1(e.target.value)}
            hint="8자 이상 — 영문·숫자·기호 조합 권장"
            required
            autoComplete="new-password"
          />
          <Input
            type="password"
            name="new_password_confirm"
            label="새 비밀번호 확인"
            value={next2}
            onChange={(e) => setNext2(e.target.value)}
            required
            autoComplete="new-password"
          />
          {pwMessage && (
            <p
              className={`text-xs ${
                pwMessage.kind === 'ok' ? 'text-green-700' : 'text-red-600'
              }`}
            >
              {pwMessage.text}
            </p>
          )}
          <Button
            type="submit"
            loading={pwLoading}
            disabled={!current || !next1 || !next2}
          >
            비밀번호 변경
          </Button>
        </form>
      </Card>

      <Card>
        <CardHeader
          title="2단계 인증 (MFA)"
          description="TOTP 인증기 앱(Google Authenticator, Authy 등)으로 2단계 인증 강화."
          action={<Badge tone="warn">준비 중</Badge>}
        />
        <div className="text-sm text-gray-600">
          <p>
            MFA 등록·해제 기능은 AuthFusion REST API gap 보완 후 활성화됩니다
            (예정: <code className="text-xs bg-gray-100 px-1 rounded">/api/v1/users/me/mfa/enroll</code> 등).
          </p>
          <p className="text-xs text-gray-500 mt-2">
            그 사이엔 <code className="text-xs bg-gray-100 px-1 rounded">console.aines.kr/me</code>에서 IdP 자체 self-service로 등록할 수 있습니다.
          </p>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="이메일 변경"
          description="AuthFusion 등록 이메일 변경 + 확인 메일 발송."
          action={<Badge tone="warn">준비 중</Badge>}
        />
        <p className="text-sm text-gray-600">
          현재 이메일: <span className="font-medium">{user.email ?? '(미등록)'}</span>
        </p>
      </Card>
    </div>
  );
}
