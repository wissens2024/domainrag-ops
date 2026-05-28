'use client';

import { useEffect, useRef, useState } from 'react';
import { useRouter, useSearchParams } from 'next/navigation';
import { API_BASE, getCurrentUser, postLoginDestination } from '@/lib/api';

type Status = 'exchanging' | 'success' | 'error';

interface ErrorState {
  code: string;
  description: string;
}

// AuthFusion이 일부 사용자 정책 위반을 OIDC error로 반환할 때 사용자가 다음
// 액션을 알 수 있도록 분기. spec: docs/integration/authfusion-self-service-v1.md
function categorizeError(err: ErrorState): {
  title: string;
  body: string;
  primary?: { label: string; href: string };
} {
  const desc = (err.description || '').toLowerCase();

  // MFA enrollment 필요 — 로그인 전이라 RP token 없음. IdP 콘솔에서 등록 필요.
  if (
    err.code === 'login_required' &&
    (desc.includes('mfa') || desc.includes('enrollment'))
  ) {
    return {
      title: 'MFA 등록 필요',
      body: '이 서비스는 2단계 인증(MFA) 등록이 필수입니다. AuthFusion 콘솔에서 인증기 앱(Google Authenticator 등)을 등록한 뒤 다시 로그인하세요.',
      primary: {
        label: 'AuthFusion 콘솔에서 MFA 등록',
        href: 'https://console.aines.kr/me',
      },
    };
  }

  // 비밀번호 만료 — IdP 콘솔에서 변경 필요
  if (err.code === 'login_required' && desc.includes('password')) {
    return {
      title: '비밀번호 변경 필요',
      body: '비밀번호 만료·정책 위반으로 로그인이 거절되었습니다. AuthFusion 콘솔에서 변경 후 다시 시도하세요.',
      primary: {
        label: 'AuthFusion 콘솔에서 비밀번호 변경',
        href: 'https://console.aines.kr/me',
      },
    };
  }

  if (err.code === 'access_denied') {
    return {
      title: '접근 거부',
      body: '이 서비스에 대한 접근 권한이 없습니다. 관리자에게 문의하세요.',
    };
  }

  if (err.code === 'invalid_or_expired_state') {
    return {
      title: '세션이 만료되었습니다',
      body: '로그인 흐름이 너무 오래 걸려 만료되었습니다. 다시 시도해 주세요.',
    };
  }

  return {
    title: '로그인 실패',
    body: err.description || err.code || '알 수 없는 오류가 발생했습니다.',
  };
}

export default function AuthCallbackPage() {
  const router = useRouter();
  const params = useSearchParams();
  const [status, setStatus] = useState<Status>('exchanging');
  const [errorState, setErrorState] = useState<ErrorState | null>(null);
  const exchangedRef = useRef<string | null>(null);

  useEffect(() => {
    const code = params.get('code');
    const state = params.get('state');
    const oidcError = params.get('error');

    if (oidcError) {
      setStatus('error');
      setErrorState({
        code: oidcError,
        description: params.get('error_description') ?? '',
      });
      return;
    }
    if (!code || !state) {
      setStatus('error');
      setErrorState({ code: 'missing_code_or_state', description: '' });
      return;
    }

    const key = `${code}:${state}`;
    if (exchangedRef.current === key) return;
    if (typeof window !== 'undefined') {
      const sessionKey = `auth_cb_${key}`;
      if (window.sessionStorage.getItem(sessionKey)) return;
      window.sessionStorage.setItem(sessionKey, '1');
    }
    exchangedRef.current = key;

    const url = `${API_BASE}/api/auth/callback?code=${encodeURIComponent(code)}&state=${encodeURIComponent(state)}`;
    fetch(url, { credentials: 'include' })
      .then(async (res) => {
        if (!res.ok) {
          const body = await res.json().catch(() => ({}));
          const detail = body?.detail || {};
          throw {
            code: detail?.error || `http_${res.status}`,
            description: detail?.reason ?? detail?.detail ?? '',
          } as ErrorState;
        }
        const body = (await res.json()) as { domain_id: string };
        setStatus('success');
        // ADR-016 — 역할-aware 착지. 콜백은 domain_id만 알므로 /api/auth/me로 역할을
        // 받아 분기(관리자는 콘솔 직행, 사용자는 채팅). 실패 시 채팅으로 안전 fallback.
        try {
          const me = await getCurrentUser();
          window.location.replace(postLoginDestination(me));
        } catch {
          window.location.replace(`/${body.domain_id}/chat`);
        }
      })
      .catch((err) => {
        setStatus('error');
        if (err && typeof err === 'object' && 'code' in err) {
          setErrorState(err as ErrorState);
        } else {
          setErrorState({
            code: 'unknown',
            description: err instanceof Error ? err.message : String(err),
          });
        }
      });
  }, [params, router]);

  return (
    <main className="min-h-screen flex items-center justify-center bg-gray-50 p-6 font-sans antialiased">
      <div className="w-full max-w-md">
        {status === 'exchanging' && (
          <div className="text-center animate-fade-in">
            <div className="inline-block w-8 h-8 border-3 border-gray-300 border-t-gray-900 rounded-full animate-spin mb-4" />
            <h1 className="text-base font-semibold text-gray-900">로그인 처리 중</h1>
            <p className="text-xs text-gray-500 mt-1">잠시만 기다려 주세요.</p>
          </div>
        )}
        {status === 'success' && (
          <div className="text-center animate-fade-in">
            <div className="w-10 h-10 mx-auto bg-green-100 text-green-700 rounded-full flex items-center justify-center text-lg mb-3">
              ✓
            </div>
            <h1 className="text-base font-semibold text-gray-900">로그인 완료</h1>
            <p className="text-xs text-gray-500 mt-1">메인 화면으로 이동합니다.</p>
          </div>
        )}
        {status === 'error' && errorState && (
          <ErrorCard err={errorState} />
        )}
      </div>
    </main>
  );
}

function ErrorCard({ err }: { err: ErrorState }) {
  const info = categorizeError(err);
  return (
    <div className="bg-white border border-gray-200 rounded-2xl p-6 shadow-card animate-slide-up">
      <div className="w-10 h-10 bg-amber-100 text-amber-700 rounded-full flex items-center justify-center text-lg mb-4">
        !
      </div>
      <h1 className="text-base font-semibold text-gray-900 mb-2">{info.title}</h1>
      <p className="text-sm text-gray-600 leading-relaxed mb-5">{info.body}</p>

      <div className="space-y-2">
        {info.primary && (
          <a
            href={info.primary.href}
            target="_blank"
            rel="noopener noreferrer"
            className="block w-full text-center px-4 py-2.5 bg-gray-900 text-white rounded-lg text-sm font-medium hover:bg-gray-800 transition-colors"
          >
            {info.primary.label} →
          </a>
        )}
        <a
          href="/"
          className="block w-full text-center px-4 py-2.5 border border-gray-300 rounded-lg text-sm text-gray-700 hover:bg-gray-50 transition-colors"
        >
          처음 화면으로
        </a>
      </div>

      <details className="mt-5">
        <summary className="text-[11px] text-gray-400 cursor-pointer hover:text-gray-600">
          상세 정보
        </summary>
        <div className="mt-2 bg-gray-50 border border-gray-200 rounded-lg p-2.5 font-mono text-[10px] text-gray-700">
          <div>
            <span className="text-gray-400">code:</span> {err.code}
          </div>
          {err.description && (
            <div className="mt-0.5 break-all">
              <span className="text-gray-400">description:</span> {err.description}
            </div>
          )}
        </div>
      </details>
    </div>
  );
}
