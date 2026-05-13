import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'DomainRAG Ops',
  description: '폐쇄망 멀티테넌트 RAG 플랫폼',
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
