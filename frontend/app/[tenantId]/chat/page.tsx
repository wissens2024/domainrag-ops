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
import Link from 'next/link';
import { useParams } from 'next/navigation';
import useSWR from 'swr';
import AnswerCard from '@/components/AnswerCard';
import CitationPanel from '@/components/CitationPanel';
import ModeSelector from '@/components/ModeSelector';
import {
  chat,
  chatStream,
  deleteConversation,
  getConversation,
  listConversations,
  swrFetcher,
} from '@/lib/api';
import type { ChatResponse, Citation, Conversation, UserContext } from '@/lib/types';

export default function ChatPage() {
  const params = useParams<{ tenantId: string }>();
  const tenantId = params.tenantId;

  const [question, setQuestion] = useState('');
  // thread: 사용자 메시지 + assistant 응답 누적 (ChatGPT 스타일)
  type ThreadItem =
    | { role: 'user'; content: string }
    | { role: 'assistant'; response: ChatResponse };
  const [thread, setThread] = useState<ThreadItem[]>([]);
  // 진행 중 streaming 응답 (token 누적용 — onComplete 시 thread에 commit)
  const [streamingResponse, setStreamingResponse] = useState<ChatResponse | null>(null);
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentConversationId, setCurrentConversationId] = useState<string | null>(null);
  const [mode, setMode] = useState<'structured' | 'streaming'>('structured');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // RBAC 메뉴 노출용 — admin/platform_admin 사용자에게만 콘솔 진입 링크 표시.
  const { data: me } = useSWR<UserContext>('/api/auth/me', swrFetcher, {
    shouldRetryOnError: false,
  });

  const refreshConversations = async () => {
    try {
      const list = await listConversations(tenantId, { page_size: 50 });
      setConversations(list?.items ?? []);
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
    const q = question.trim();
    if (!q) return;
    setLoading(true);
    setError(null);
    // user 메시지 즉시 thread에 push
    setThread((prev) => [...prev, { role: 'user', content: q }]);
    setQuestion('');
    try {
      if (mode === 'streaming') {
        // chat_streaming: SSE, citation 없음 (ADR-013 §6). token을 누적해 진행 응답 표시,
        // onComplete 시 thread에 commit.
        let acc = '';
        let convId = currentConversationId;
        const initial: ChatResponse = {
          status: 'success',
          conversation_id: convId ?? '',
          message_id: '',
          answer: '',
          answer_segments: [{ text: '', citations: [] }],
          citations: [],
          metadata: {
            ui_mode: 'chat_streaming',
            llm_model: '(streaming)',
            latency_ms: 0,
            confidence: 0,
          },
        };
        setStreamingResponse(initial);
        await chatStream(
          tenantId,
          { question: q, conversation_id: currentConversationId ?? undefined },
          {
            onToken: (t) => {
              acc += t;
              setStreamingResponse((prev) =>
                prev && prev.status === 'success'
                  ? { ...prev, answer: acc, answer_segments: [{ text: acc, citations: [] }] }
                  : prev,
              );
            },
            onComplete: (payload) => {
              const meta = (payload.metadata ?? {}) as Record<string, unknown>;
              convId = (meta.conversation_id as string) ?? convId;
              if (convId) setCurrentConversationId(convId);
              const finalRes: ChatResponse = {
                ...initial,
                answer: acc,
                answer_segments: [{ text: acc, citations: [] }],
                message_id: String(payload.message_id ?? ''),
                conversation_id: convId ?? '',
                metadata: { ...initial.metadata, ...meta } as ChatResponse['metadata'],
              };
              setThread((prev) => [...prev, { role: 'assistant', response: finalRes }]);
              setStreamingResponse(null);
            },
            onFallback: (payload) => {
              setError(`fallback: ${JSON.stringify(payload)}`);
              setStreamingResponse(null);
            },
            onError: (payload) => {
              setError(`stream error: ${JSON.stringify(payload)}`);
              setStreamingResponse(null);
            },
          },
        );
        void refreshConversations();
      } else {
        const res = await chat(tenantId, {
          question: q,
          conversation_id: currentConversationId ?? undefined,
          ui_mode_request: mode,
        });
        setThread((prev) => [...prev, { role: 'assistant', response: res }]);
        setCurrentConversationId(res.conversation_id);
        void refreshConversations();
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : '알 수 없는 오류');
    } finally {
      setLoading(false);
    }
  };

  const handleNewConversation = () => {
    setCurrentConversationId(null);
    setThread([]);
    setStreamingResponse(null);
    setSelectedCitation(null);
    setQuestion('');
    setError(null);
  };

  const handleSelectConversation = async (cid: string) => {
    setCurrentConversationId(cid);
    setLoading(true);
    setError(null);
    setStreamingResponse(null);
    try {
      const detail = await getConversation(tenantId, cid);
      // 전체 messages를 thread로 복원 (user → assistant 순서, ADR-017 §4)
      const restored: ThreadItem[] = (detail.messages ?? []).map((m) =>
        m.role === 'user'
          ? { role: 'user' as const, content: m.content }
          : {
              role: 'assistant' as const,
              response: {
                status: 'success',
                conversation_id: cid,
                message_id: m.request_id ?? '',
                answer: m.content,
                answer_segments: [{ text: m.content, citations: [] }],
                citations: [],
                metadata: {
                  ui_mode: 'chat_structured',
                  llm_model: '(history)',
                  latency_ms: 0,
                  confidence: 0,
                },
              } as ChatResponse,
            },
      );
      setThread(restored);
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
        {/* RBAC 메뉴 (ADR-016 Y9) — admin role 보유자에게만 노출 */}
        {(me?.is_admin || me?.is_platform_admin) && (
          <div className="mb-3 space-y-1">
            <Link
              href={`/${tenantId}/admin/dashboard`}
              className="block px-3 py-1.5 border border-gray-300 rounded text-xs text-center hover:bg-gray-50"
            >
              🏢 관리자 콘솔
            </Link>
            {me?.is_platform_admin && (
              <Link
                href="/platform/admin/tenants"
                className="block px-3 py-1.5 border border-gray-300 rounded text-xs text-center hover:bg-gray-50"
              >
                🌐 Platform Admin
              </Link>
            )}
          </div>
        )}
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

        <div className="flex-1 overflow-y-auto p-4 space-y-4">
          {thread.length === 0 && !loading && !streamingResponse && (
            <div className="text-center text-gray-400 mt-12 text-sm">
              질문을 입력해 대화를 시작하세요.
            </div>
          )}
          {thread.map((item, i) =>
            item.role === 'user' ? (
              <div key={i} className="flex justify-end">
                <div className="max-w-[75%] bg-blue-600 text-white rounded-2xl px-4 py-2 whitespace-pre-wrap">
                  {item.content}
                </div>
              </div>
            ) : (
              <div key={i} className="flex justify-start">
                <div className="max-w-[85%]">
                  <AnswerCard
                    response={item.response}
                    tenantId={tenantId}
                    onCitationClick={setSelectedCitation}
                  />
                </div>
              </div>
            ),
          )}
          {streamingResponse && (
            <div className="flex justify-start">
              <div className="max-w-[85%]">
                <AnswerCard
                  response={streamingResponse}
                  tenantId={tenantId}
                  onCitationClick={setSelectedCitation}
                />
              </div>
            </div>
          )}
          {loading && !streamingResponse && (
            <div className="text-gray-500 text-sm">답변 생성 중…</div>
          )}
          {error && <div className="text-red-600 text-sm">오류: {error}</div>}
        </div>

        <form
          onSubmit={handleSubmit}
          className="p-4 border-t border-gray-200 bg-white sticky bottom-0"
        >
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

      {/* 우측 — Citation Panel (citation 클릭 시만 노출) */}
      {selectedCitation && (
        <aside className="w-96 border-l border-gray-200 p-4 overflow-y-auto relative">
          <button
            onClick={() => setSelectedCitation(null)}
            className="absolute top-2 right-2 text-gray-500 hover:text-gray-800 text-xl"
            aria-label="닫기"
          >
            ✕
          </button>
          <CitationPanel citation={selectedCitation} />
        </aside>
      )}
    </div>
  );
}
