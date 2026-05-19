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

  // 미인증 — backend authorize endpoint로 302. backend가 다시 IdP로 302.
  // /platform/admin/*은 tenant=platform → backend 측 client_id 매핑이 필요.
  // 단일 platform_admin client_id가 등록되어 있다고 가정 (ADR-018 §7).
  //
  // NEXT_PUBLIC_API_URL이 설정되어 있으면 그 절대 URL을 사용 (dev에서 frontend
  // 포트 3010 ≠ backend 8001). 운영(115)은 same-origin이라 NEXT_PUBLIC_API_URL을
  // 빈 문자열로 두면 host 그대로.
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
