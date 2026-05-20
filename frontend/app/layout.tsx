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
      <head>
        <link
          rel="stylesheet"
          href="https://cdn.jsdelivr.net/gh/orioncactus/pretendard@v1.3.9/dist/web/variable/pretendardvariable.min.css"
        />
      </head>
      <body className="font-sans antialiased text-gray-900">{children}</body>
    </html>
  );
}
