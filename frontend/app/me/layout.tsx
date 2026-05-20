// 기존 /me/* path는 /account/*로 정렬됨. 본 layout은 redirect만 담당.
export default function MeRedirectLayout({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}
