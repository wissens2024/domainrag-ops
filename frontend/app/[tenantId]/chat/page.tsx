/**
 * 사용자 채팅 화면 — `/{tenantId}/chat` (ADR-016 §2).
 *
 * 기능:
 *   - chat_structured / chat_streaming 모드 전환 (ModeSelector)
 *   - 좌측: 대화 목록 (Conversation API 연동)
 *   - 중앙: 답변 + 4-type citation marker
 *   - 우측: CitationPanel
 */
'use client';

import { useEffect, useState } from 'react';
import { useParams } from 'next/navigation';
import AnswerCard from '@/components/AnswerCard';
import CitationPanel from '@/components/CitationPanel';
import ModeSelector from '@/components/ModeSelector';
import {
  chat,
  deleteConversation,
  getConversation,
  listConversations,
} from '@/lib/api';
import type { ChatResponse, Citation, Conversation } from '@/lib/types';

export default function ChatPage() {
  const params = useParams<{ tenantId: string }>();
  const tenantId = params.tenantId;

  const [question, setQuestion] = useState('');
  const [response, setResponse] = useState<ChatResponse | null>(null);
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentConversationId, setCurrentConversationId] = useState<string | null>(null);
  const [mode, setMode] = useState<'structured' | 'streaming'>('structured');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const refreshConversations = async () => {
    try {
      const list = await listConversations(tenantId, { page_size: 50 });
      setConversations(list.items);
    } catch {
      // ignore — 권한 없거나 빈 응답
    }
  };

  useEffect(() => {
    void refreshConversations();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [tenantId]);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!question.trim()) return;
    setLoading(true);
    setError(null);
    try {
      const res = await chat(tenantId, {
        question,
        conversation_id: currentConversationId ?? undefined,
        ui_mode_request: mode,
      });
      setResponse(res);
      setCurrentConversationId(res.conversation_id);
      setSelectedCitation(null);
      setQuestion('');
      void refreshConversations();
    } catch (err) {
      setError(err instanceof Error ? err.message : '알 수 없는 오류');
    } finally {
      setLoading(false);
    }
  };

  const handleNewConversation = () => {
    setCurrentConversationId(null);
    setResponse(null);
    setSelectedCitation(null);
    setQuestion('');
    setError(null);
  };

  const handleSelectConversation = async (cid: string) => {
    setCurrentConversationId(cid);
    setLoading(true);
    setError(null);
    try {
      const detail = await getConversation(tenantId, cid);
      // 마지막 assistant 메시지를 노출 (전체 messages history는 별도 panel 추가 후보)
      const lastAssistant = [...detail.messages]
        .reverse()
        .find((m) => m.role === 'assistant');
      if (lastAssistant) {
        // 메시지 본문만 단순 응답 형태로 흉내 (citations는 chat_log에서 별도 hydrate 필요)
        setResponse({
          status: 'success',
          conversation_id: cid,
          message_id: lastAssistant.request_id ?? '',
          answer: lastAssistant.content,
          answer_segments: [{ text: lastAssistant.content, citations: [] }],
          citations: [],
          metadata: {
            ui_mode: 'chat_structured',
            llm_model: '(history)',
            latency_ms: 0,
            confidence: 0,
          },
        });
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '대화 로드 실패');
    } finally {
      setLoading(false);
    }
  };

  const handleDeleteConversation = async (cid: string) => {
    if (!confirm('이 대화를 삭제하시겠습니까?')) return;
    try {
      await deleteConversation(tenantId, cid);
      if (currentConversationId === cid) {
        handleNewConversation();
      }
      void refreshConversations();
    } catch (err) {
      setError(err instanceof Error ? err.message : '삭제 실패');
    }
  };

  return (
    <div className="flex h-screen">
      {/* 좌측 — 대화 목록 */}
      <aside className="w-64 border-r border-gray-200 p-4 flex flex-col">
        <h2 className="font-bold mb-3">대화 목록</h2>
        <button
          onClick={handleNewConversation}
          className="w-full px-3 py-2 bg-blue-600 text-white rounded text-sm mb-3"
        >
          + 새 대화
        </button>
        <div className="flex-1 overflow-y-auto space-y-1">
          {conversations.length === 0 && (
            <p className="text-xs text-gray-400">아직 대화가 없습니다.</p>
          )}
          {conversations.map((c) => (
            <div
              key={c.conversation_id}
              className={`group relative px-2 py-2 rounded text-sm cursor-pointer ${
                currentConversationId === c.conversation_id
                  ? 'bg-blue-100'
                  : 'hover:bg-gray-100'
              }`}
              onClick={() => handleSelectConversation(c.conversation_id)}
            >
              <div className="truncate font-medium">
                {c.title || c.conversation_id.slice(0, 12)}
              </div>
              <div className="text-xs text-gray-500">
                {new Date(c.updated_at).toLocaleString('ko-KR')} ·{' '}
                {c.message_count}건
              </div>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  void handleDeleteConversation(c.conversation_id);
                }}
                className="absolute right-1 top-1 hidden group-hover:block text-red-500 text-xs px-1"
                title="삭제"
              >
                ✕
              </button>
            </div>
          ))}
        </div>
      </aside>

      {/* 중앙 — 채팅 영역 */}
      <main className="flex-1 flex flex-col">
        <header className="p-4 border-b border-gray-200 flex justify-between items-center">
          <h1 className="font-bold">DomainRAG · {tenantId}</h1>
          <div className="flex gap-3 items-center">
            <ModeSelector value={mode} onChange={setMode} />
            <a
              href={`/${tenantId}/admin/dashboard`}
              className="text-xs px-3 py-1 border rounded text-gray-700 hover:bg-gray-50"
            >
              관리자
            </a>
          </div>
        </header>

        <div className="flex-1 overflow-y-auto p-4">
          {loading && <div className="text-gray-500">답변 생성 중...</div>}
          {error && <div className="text-red-600">오류: {error}</div>}
          {response && (
            <AnswerCard
              response={response}
              tenantId={tenantId}
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
