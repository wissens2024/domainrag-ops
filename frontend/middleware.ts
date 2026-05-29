/**
 * Next.js middleware — ADR-016 §3 + ADR-018 §2/§6 server-side auth gate.
 *
 * 클라이언트 layer:
 *   - `domainrag_access` httpOnly 쿠키가 있으면 protected route를 통과시킨다
 *     (실제 JWT 검증은 backend가 endpoint마다 수행 — 두 layer 모두 의무).
 *   - 쿠키가 없고 protected route면 `/api/auth/authorize/<tenant>?redirect=1`로
 *     302 redirect. backend가 다시 IdP authorize URL로 302를 이어 준다.
 *
 * Protected:
 *   - `/<tenant>/chat`           (USER)
 *   - `/<tenant>/admin/*`        (ADMIN)
 *   - `/platform/admin/*`        (PLATFORM_ADMIN)
 *
 * Public:
 *   - `/`                        — 랜딩
 *   - `/auth/callback`           — OIDC callback (쿠키 set 전)
 *   - `/_next/*`, `/favicon.ico` — Next.js 자산
 *
 * RBAC role 검증(USER vs ADMIN vs PLATFORM_ADMIN)은 backend endpoint가 담당.
 * middleware는 "로그인됐는가"만 본다.
 */
import { NextRequest, NextResponse } from 'next/server';

const ACCESS_COOKIE = 'domainrag_access';
const REFRESH_COOKIE = 'domainrag_refresh';
const TENANT_CHAT_RE = /^\/([^/]+)\/chat(?:\/|$)/;
const TENANT_ADMIN_RE = /^\/([^/]+)\/admin(?:\/|$)/;
const PLATFORM_ADMIN_RE = /^\/platform\/admin(?:\/|$)/;

function _protectedTenant(pathname: string): string | null {
  if (PLATFORM_ADMIN_RE.test(pathname)) return 'platform';
  const m = pathname.match(TENANT_CHAT_RE) || pathname.match(TENANT_ADMIN_RE);
  if (!m) return null;
  const tenant = m[1];
  // /platform/admin은 위에서 처리. 그 외 reserved 단어 제외.
  if (tenant === 'platform' || tenant === 'auth' || tenant === '_next') return null;
  return tenant;
}

export function middleware(req: NextRequest) {
  const pathname = req.nextUrl.pathname;
  const tenant = _protectedTenant(pathname);
  if (!tenant) return NextResponse.next();

  const hasAccess = req.cookies.get(ACCESS_COOKIE)?.value;
  if (hasAccess) return NextResponse.next();

  // ADR-018 §6 — access 쿠키가 만료(짧은 TTL)됐어도 refresh 쿠키가 살아있으면 로그인으로
  // 튕기지 않는다. 페이지를 통과시키면 api.ts 401 interceptor가 첫 요청에서 조용히 refresh →
  // 새 access 쿠키 발급. 이렇게 해야 access TTL마다 재로그인하는 문제가 사라진다.
  // (refresh 쿠키 수명 = AuthFusion refresh_expires_in. 둘 다 없을 때만 실제 로그아웃.)
  const hasRefresh = req.cookies.get(REFRESH_COOKIE)?.value;
  if (hasRefresh) return NextResponse.next();

  // 미인증 — /platform/admin/* 은 backend가 'platform' tenant를 인식 안 함 (special).
  // /console 페이지가 자체 SSO 흐름(default tenant authorize)을 가지므로 그쪽으로 보낸다.
  if (tenant === 'platform') {
    const url = req.nextUrl.clone();
    url.pathname = '/console';
    url.search = '';
    return NextResponse.redirect(url, 302);
  }

  // tenant scope (/security/chat, /security/admin/*) — backend authorize로.
  // NEXT_PUBLIC_API_URL이 설정되어 있으면 절대 URL (dev). 운영은 same-origin.
  const apiBase = process.env.NEXT_PUBLIC_API_URL || '';
  if (apiBase) {
    return NextResponse.redirect(
      `${apiBase}/api/auth/authorize/${tenant}?redirect=1`,
      302,
    );
  }
  const url = req.nextUrl.clone();
  url.pathname = `/api/auth/authorize/${tenant}`;
  url.search = '?redirect=1';
  return NextResponse.redirect(url, 302);
}

export const config = {
  matcher: [
    /*
     * 정적 자원과 _next 내부 경로, /api/*, /auth/callback 제외.
     * Next.js matcher는 negative lookahead만 지원 — protected 매칭은 함수가 한다.
     */
    '/((?!_next/static|_next/image|favicon.ico|api/|auth/callback).*)',
  ],
};
