/**
 * 사용자 채팅 화면 — `/{tenantId}/chat` (ADR-016 §2).
 */
'use client';

import { useState } from 'react';
import { useParams } from 'next/navigation';
import AnswerCard from '@/components/AnswerCard';
import CitationPanel from '@/components/CitationPanel';
import { chat } from '@/lib/api';
import type { ChatResponse, Citation } from '@/lib/types';

export default function ChatPage() {
  const params = useParams<{ tenantId: string }>();
  const tenantId = params.tenantId;

  const [question, setQuestion] = useState('');
  const [response, setResponse] = useState<ChatResponse | null>(null);
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!question.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await chat(tenantId, { question });
      setResponse(res);
      setSelectedCitation(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : '알 수 없는 오류');
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="flex h-screen">
      {/* 좌측 — 대화 목록 (stub) */}
      <aside className="w-64 border-r border-gray-200 p-4">
        <h2 className="font-bold mb-4">대화 목록</h2>
        <button className="w-full px-3 py-2 bg-blue-600 text-white rounded text-sm">+ 새 대화</button>
      </aside>

      {/* 중앙 — 채팅 영역 */}
      <main className="flex-1 flex flex-col">
        <header className="p-4 border-b border-gray-200">
          <h1 className="font-bold">DomainRAG · {tenantId}</h1>
        </header>

        <div className="flex-1 overflow-y-auto p-4">
          {loading && <div className="text-gray-500">답변 생성 중...</div>}
          {error && <div className="text-red-600">오류: {error}</div>}
          {response && (
            <AnswerCard
              response={response}
              onCitationClick={setSelectedCitation}
            />
          )}
        </div>

        <form onSubmit={handleSubmit} className="p-4 border-t border-gray-200">
          <div className="flex gap-2">
            <input
              type="text"
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              placeholder="질문을 입력하세요..."
              className="flex-1 px-3 py-2 border border-gray-300 rounded"
              disabled={loading}
            />
            <button
              type="submit"
              disabled={loading || !question.trim()}
              className="px-4 py-2 bg-blue-600 text-white rounded disabled:bg-gray-400"
            >
              전송
            </button>
          </div>
        </form>
      </main>

      {/* 우측 — Citation Panel */}
      <aside className="w-96 border-l border-gray-200 p-4 overflow-y-auto">
        <CitationPanel citation={selectedCitation} />
      </aside>
    </div>
  );
}
