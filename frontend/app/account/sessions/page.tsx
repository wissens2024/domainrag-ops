'use client';

import useSWR, { mutate } from 'swr';
import { useState } from 'react';
import Card, { CardHeader } from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import Badge from '@/components/ui/Badge';
import { account, ApiError } from '@/lib/api';

const KEY = 'account.sessions';

function formatTime(iso: string): string {
  try {
    return new Date(iso).toLocaleString('ko-KR', {
      year: 'numeric',
      month: '2-digit',
      day: '2-digit',
      hour: '2-digit',
      minute: '2-digit',
    });
  } catch {
    return iso;
  }
}

function shortUA(ua: string): string {
  // 간단 추출 — Chrome on Windows / Safari on iOS 같이
  const browser = /Edg\//.test(ua)
    ? 'Edge'
    : /Chrome\//.test(ua)
      ? 'Chrome'
      : /Safari\//.test(ua)
        ? 'Safari'
        : /Firefox\//.test(ua)
          ? 'Firefox'
          : 'Browser';
  const os = /Windows/.test(ua)
    ? 'Windows'
    : /Mac OS X/.test(ua)
      ? 'macOS'
      : /Android/.test(ua)
        ? 'Android'
        : /iPhone|iPad/.test(ua)
          ? 'iOS'
          : /Linux/.test(ua)
            ? 'Linux'
            : 'Unknown';
  return `${browser} · ${os}`;
}

export default function SessionsPage() {
  const { data, error, isLoading } = useSWR(KEY, () => account.getSessions());
  const [pending, setPending] = useState<string | null>(null);
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const revoke = async (sessionId: string) => {
    if (!confirm('이 세션을 종료하시겠습니까?')) return;
    setPending(sessionId);
    setMsg(null);
    try {
      await account.revokeSession(sessionId);
      setMsg({ kind: 'ok', text: '세션이 종료되었습니다.' });
      void mutate(KEY);
    } catch (e) {
      const text =
        e instanceof ApiError
          ? ((e.detail as Record<string, unknown>)?.message as string) ??
            `HTTP ${e.status}`
          : (e as Error).message;
      setMsg({ kind: 'err', text });
    } finally {
      setPending(null);
    }
  };

  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          title="활성 세션"
          description="이 계정으로 로그인된 device · 브라우저 목록. 의심스러운 세션은 즉시 종료하세요."
        />
        {msg && (
          <p
            className={`text-xs mb-4 ${
              msg.kind === 'ok' ? 'text-green-700' : 'text-red-600'
            }`}
          >
            {msg.text}
          </p>
        )}
        {isLoading && <p className="text-xs text-gray-400">로드 중…</p>}
        {error && (
          <p className="text-xs text-red-600">
            로드 실패: {(error as Error).message}
          </p>
        )}
        {data && data.length === 0 && (
          <p className="text-xs text-gray-400">활성 세션이 없습니다.</p>
        )}
        {data && data.length > 0 && (
          <ul className="divide-y divide-gray-100">
            {data.map((s) => (
              <li
                key={s.sessionId}
                className="py-3 flex items-center justify-between gap-3"
              >
                <div className="min-w-0 flex-1">
                  <div className="flex items-center gap-2">
                    <span className="text-sm font-medium text-gray-900">
                      {shortUA(s.userAgent)}
                    </span>
                    <Badge tone="neutral">{s.ipAddress}</Badge>
                  </div>
                  <div className="text-[11px] text-gray-500 mt-1 flex flex-wrap gap-x-3 gap-y-0.5">
                    <span>최초 로그인: {formatTime(s.createdAt)}</span>
                    <span>최근 활동: {formatTime(s.lastActivityAt)}</span>
                    <span>만료: {formatTime(s.expiresAt)}</span>
                  </div>
                  <div className="text-[10px] text-gray-400 font-mono mt-1 truncate">
                    {s.sessionId}
                  </div>
                </div>
                <Button
                  variant="danger"
                  size="sm"
                  loading={pending === s.sessionId}
                  onClick={() => revoke(s.sessionId)}
                >
                  종료
                </Button>
              </li>
            ))}
          </ul>
        )}
      </Card>

      <Card>
        <CardHeader
          title="다른 device 일괄 로그아웃"
          description="AuthFusion의 'logout-all' endpoint는 현재 self-service API에 미포함. 위 목록에서 개별 종료하세요."
        />
        <p className="text-xs text-gray-500">
          spec에 포함되면 추가합니다 (참고:{' '}
          <code className="text-[11px] bg-gray-100 px-1 rounded">
            docs/integration/authfusion-self-service-v1.md
          </code>
          ).
        </p>
      </Card>
    </div>
  );
}
