'use client';

import { useEffect, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';

type Status = 'exchanging' | 'success' | 'error';

export default function AuthCallbackPage() {
  const router = useRouter();
  const params = useSearchParams();
  const [status, setStatus] = useState<Status>('exchanging');
  const [errorMsg, setErrorMsg] = useState<string | null>(null);

  useEffect(() => {
    const code = params.get('code');
    const state = params.get('state');
    const oidcError = params.get('error');

    if (oidcError) {
      setStatus('error');
      setErrorMsg(`${oidcError}: ${params.get('error_description') ?? ''}`);
      return;
    }
    if (!code || !state) {
      setStatus('error');
      setErrorMsg('missing_code_or_state');
      return;
    }

    const url = `/api/auth/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`;
    fetch(url, { credentials: 'include' })
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          throw new Error(body?.detail?.error || `HTTP ${res.status}`);
        }
        const body = (await res.json()) as { tenant_id: string };
        setStatus('success');
        router.replace(`/${body.tenant_id}/chat`);
      })
      .catch((err) => {
        setStatus('error');
        setErrorMsg(err instanceof Error ? err.message : String(err));
      });
  }, [params, router]);

  return (
    <main className="min-h-screen flex flex-col items-center justify-center p-8">
      {status === 'exchanging' && (
        <>
          <h1 className="text-xl font-semibold mb-2">로그인 처리 중…</h1>
          <p className="text-sm text-gray-500">잠시만 기다려 주세요.</p>
        </>
      )}
      {status === 'success' && (
        <>
          <h1 className="text-xl font-semibold mb-2">로그인 완료</h1>
          <p className="text-sm text-gray-500">메인 화면으로 이동합니다.</p>
        </>
      )}
      {status === 'error' && (
        <>
          <h1 className="text-xl font-semibold mb-2 text-red-600">로그인 실패</h1>
          <p className="text-sm text-gray-600 mb-4">{errorMsg}</p>
          <a href="/" className="text-blue-600 underline text-sm">
            처음 화면으로
          </a>
        </>
      )}
    </main>
  );
}
