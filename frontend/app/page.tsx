import Link from 'next/link';

/**
 * 랜딩 페이지 — 사용자 SSO 후 자동으로 자기 tenant로 redirect.
 * Dev 모드에서는 default tenant 링크 표시 (ADR-018 mock).
 */
export default function Home() {
  const defaultTenant = process.env.NEXT_PUBLIC_DEFAULT_TENANT || 'security';
  return (
    <main className="min-h-screen flex flex-col items-center justify-center p-8">
      <h1 className="text-3xl font-bold mb-2">DomainRAG Ops</h1>
      <p className="text-gray-600 mb-8">폐쇄망 멀티테넌트 RAG 플랫폼</p>

      <div className="space-y-2 w-64">
        <Link
          href={`/${defaultTenant}/chat`}
          className="block px-4 py-2 bg-blue-600 text-white rounded hover:bg-blue-700 text-center"
        >
          {defaultTenant} 채팅
        </Link>
        <Link
          href={`/${defaultTenant}/admin/dashboard`}
          className="block px-4 py-2 border border-gray-300 rounded hover:bg-gray-50 text-center"
        >
          {defaultTenant} 관리자 콘솔
        </Link>
        <Link
          href="/platform/admin/tenants"
          className="block px-4 py-2 border border-gray-300 rounded hover:bg-gray-50 text-center text-sm"
        >
          🌐 Platform Admin (PLATFORM_ADMIN role)
        </Link>
      </div>

      <p className="text-xs text-gray-500 mt-8">
        ADR-018 SSO 운영 환경에선 자동 로그인 후 자기 tenant로 redirect.
      </p>
    </main>
  );
}
