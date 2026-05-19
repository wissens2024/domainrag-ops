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
  logout,
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
  // 일반 사용자 default는 '일반 대화' (자유 대화). 문서 검색은 명시 선택 시.
  const [mode, setMode] = useState<'structured' | 'streaming'>('streaming');
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

  // 현재 대화 제목 (사이드바 row에서 가져옴)
  const currentTitle =
    conversations.find((c) => c.conversation_id === currentConversationId)
      ?.title ?? null;

  // textarea 자동 높이 + Enter 전송 (Shift+Enter 줄바꿈)
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void handleSubmit(e as unknown as React.FormEvent);
    }
  };

  return (
    <div className="flex h-screen bg-gray-50 text-gray-900">
      {/* 좌측 — 대화 목록 */}
      <aside className="w-72 border-r border-gray-200 bg-white flex flex-col">
        <div className="p-3 border-b border-gray-200">
          <button
            onClick={handleNewConversation}
            className="w-full px-3 py-2 bg-gray-900 text-white rounded-lg text-sm font-medium hover:bg-gray-800 transition-colors"
          >
            + 새 대화
          </button>
        </div>

        {(me?.is_admin || me?.is_platform_admin) && (
          <div className="px-3 py-2 border-b border-gray-200">
            <p className="text-[11px] text-gray-400 leading-tight">
              관리자 콘솔은 <code className="text-gray-600">/console</code> 로
              접속하세요 (별도 브라우저 권장).
            </p>
          </div>
        )}

        <div className="flex-1 overflow-y-auto px-2 py-2 space-y-0.5">
          {conversations.length === 0 && (
            <p className="text-xs text-gray-400 px-3 py-4 text-center">
              아직 대화 없음
            </p>
          )}
          {conversations.map((c) => (
            <div
              key={c.conversation_id}
              className={`group relative px-3 py-2 rounded-lg text-sm cursor-pointer transition-colors ${
                currentConversationId === c.conversation_id
                  ? 'bg-gray-200'
                  : 'hover:bg-gray-100'
              }`}
              onClick={() => handleSelectConversation(c.conversation_id)}
            >
              <div className="truncate font-medium pr-5">
                {c.title || '새 대화'}
              </div>
              <div className="text-xs text-gray-500 mt-0.5">
                {new Date(c.updated_at).toLocaleDateString('ko-KR')} ·{' '}
                {c.message_count}건
              </div>
              <button
                onClick={(e) => {
                  e.stopPropagation();
                  void handleDeleteConversation(c.conversation_id);
                }}
                className="absolute right-1.5 top-2 hidden group-hover:block text-gray-400 hover:text-red-500 text-xs px-1"
                title="삭제"
              >
                ✕
              </button>
            </div>
          ))}
        </div>

        {/* 사이드바 하단: user + logout */}
        {me && (
          <div className="p-3 border-t border-gray-200 text-xs text-gray-500 flex items-center justify-between gap-2">
            <div className="min-w-0">
              <div className="truncate font-medium text-gray-700">
                {me.preferred_username ?? me.email ?? me.user_id}
              </div>
              <div className="truncate">{me.tenant_id}</div>
            </div>
            <button
              onClick={async () => {
                await logout(tenantId);
                window.location.href = '/';
              }}
              className="px-2 py-1 text-[11px] text-gray-500 border border-gray-300 rounded hover:bg-gray-100 hover:text-gray-700"
              title="로그아웃"
            >
              ↩ 로그아웃
            </button>
          </div>
        )}
      </aside>

      {/* 중앙 — 채팅 영역 */}
      <main className="flex-1 flex flex-col bg-white">
        <header className="px-6 py-3 border-b border-gray-200 flex justify-between items-center bg-white">
          <div className="flex items-center gap-3 min-w-0">
            <h1 className="font-semibold text-sm text-gray-900 truncate">
              {currentTitle ?? '새 대화'}
            </h1>
            <span className="text-xs text-gray-400 hidden sm:inline">
              · {tenantId}
            </span>
          </div>
          <div className="flex gap-2 items-center">
            <ModeSelector value={mode} onChange={setMode} />
          </div>
        </header>

        <div className="flex-1 overflow-y-auto">
          <div className="max-w-3xl mx-auto px-4 py-6 space-y-6">
            {thread.length === 0 && !loading && !streamingResponse && (
              <div className="text-center text-gray-400 mt-24">
                <p className="text-lg">무엇을 도와드릴까요?</p>
                <p className="text-xs mt-2 text-gray-300">
                  질문을 입력해 대화를 시작하세요
                </p>
              </div>
            )}
            {thread.map((item, i) =>
              item.role === 'user' ? (
                <div key={i} className="flex justify-end">
                  <div className="max-w-[75%] bg-gray-900 text-white rounded-2xl px-4 py-2.5 whitespace-pre-wrap break-words text-sm leading-relaxed">
                    {item.content}
                  </div>
                </div>
              ) : (
                <div key={i} className="flex justify-start">
                  <div className="max-w-[85%] w-full">
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
                <div className="max-w-[85%] w-full">
                  <AnswerCard
                    response={streamingResponse}
                    tenantId={tenantId}
                    onCitationClick={setSelectedCitation}
                  />
                </div>
              </div>
            )}
            {loading && !streamingResponse && (
              <div className="flex justify-start">
                <div className="text-gray-400 text-sm flex items-center gap-2">
                  <span className="inline-block w-2 h-2 bg-gray-400 rounded-full animate-pulse" />
                  답변 생성 중…
                </div>
              </div>
            )}
            {error && (
              <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-lg px-3 py-2">
                오류: {error}
              </div>
            )}
          </div>
        </div>

        <form
          onSubmit={handleSubmit}
          className="border-t border-gray-200 bg-white px-4 py-3 sticky bottom-0"
        >
          <div className="max-w-3xl mx-auto flex gap-2 items-end">
            <textarea
              value={question}
              onChange={(e) => setQuestion(e.target.value)}
              onKeyDown={onKeyDown}
              placeholder="질문을 입력하세요. Shift+Enter 로 줄바꿈."
              rows={1}
              className="flex-1 px-4 py-2.5 border border-gray-300 rounded-2xl resize-none focus:outline-none focus:ring-2 focus:ring-gray-900 text-sm leading-relaxed max-h-40"
              style={{ minHeight: '44px' }}
              disabled={loading}
              onInput={(e) => {
                const el = e.currentTarget;
                el.style.height = 'auto';
                el.style.height = Math.min(el.scrollHeight, 160) + 'px';
              }}
            />
            <button
              type="submit"
              disabled={loading || !question.trim()}
              className="px-5 py-2.5 bg-gray-900 text-white rounded-2xl text-sm font-medium disabled:bg-gray-300 disabled:cursor-not-allowed hover:bg-gray-800 transition-colors"
            >
              전송
            </button>
          </div>
        </form>
      </main>

      {/* 우측 — Citation Panel (citation 클릭 시만 노출) */}
      {selectedCitation && (
        <aside className="w-96 border-l border-gray-200 bg-white overflow-y-auto relative">
          <button
            onClick={() => setSelectedCitation(null)}
            className="absolute top-3 right-3 text-gray-400 hover:text-gray-700 text-xl leading-none z-10"
            aria-label="닫기"
          >
            ✕
          </button>
          <div className="p-4">
            <CitationPanel citation={selectedCitation} />
          </div>
        </aside>
      )}
    </div>
  );
}
