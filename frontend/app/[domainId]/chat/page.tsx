/**
 * 사용자 채팅 화면 — `/{domainId}/chat` (ADR-016 §2).
 *
 * 기능 (ADR-023 — 단일 대화 파이프라인, 모드 토글 없음):
 *   - 항상 /chat 호출 → 서버가 근거 유무로 grounded/ungrounded 자동 분기
 *   - 좌측: 대화 목록 (Conversation API 연동)
 *   - 중앙: 답변 + 4-type citation marker + grounded/ungrounded 배지
 *   - 우측: CitationPanel
 */
'use client';

import { useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { useParams } from 'next/navigation';
import useSWR from 'swr';
import AnswerCard from '@/components/AnswerCard';
import CitationPanel from '@/components/CitationPanel';
import DomainSwitcher from '@/components/DomainSwitcher';
import {
  chat,
  deleteConversation,
  getConversation,
  listConversations,
  logout,
  swrFetcher,
} from '@/lib/api';
import type { ChatResponse, Citation, Conversation, UserContext } from '@/lib/types';

export default function ChatPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;

  const [question, setQuestion] = useState('');
  // thread: 사용자 메시지 + assistant 응답 누적 (ChatGPT 스타일)
  type ThreadItem =
    | { role: 'user'; content: string }
    | { role: 'assistant'; response: ChatResponse };
  const [thread, setThread] = useState<ThreadItem[]>([]);
  const [selectedCitation, setSelectedCitation] = useState<Citation | null>(null);
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [currentConversationId, setCurrentConversationId] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // RBAC 메뉴 노출용 — admin/platform_admin 사용자에게만 콘솔 진입 링크 표시.
  const { data: me } = useSWR<UserContext>('/api/auth/me', swrFetcher, {
    shouldRetryOnError: false,
  });

  const refreshConversations = async () => {
    try {
      const list = await listConversations(domainId, { page_size: 50 });
      setConversations(list?.items ?? []);
    } catch {
      // ignore — 권한 없거나 빈 응답
    }
  };

  useEffect(() => {
    void refreshConversations();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [domainId]);

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
      // ADR-023 — 단일 입구. 모드 선택 없이 항상 /chat 호출. 서버가 근거 유무로
      // grounded(citation) / ungrounded(일반 대화)를 자동 분기하고, AnswerCard가
      // 배지로 구분 표시한다.
      const res = await chat(domainId, {
        question: q,
        conversation_id: currentConversationId ?? undefined,
      });
      setThread((prev) => [...prev, { role: 'assistant', response: res }]);
      setCurrentConversationId(res.conversation_id);
      void refreshConversations();
    } catch (err) {
      setError(err instanceof Error ? err.message : '알 수 없는 오류');
    } finally {
      setLoading(false);
    }
  };

  const handleNewConversation = () => {
    setCurrentConversationId(null);
    setThread([]);
    setSelectedCitation(null);
    setQuestion('');
    setError(null);
  };

  const handleSelectConversation = async (cid: string) => {
    setCurrentConversationId(cid);
    setLoading(true);
    setError(null);
    try {
      const detail = await getConversation(domainId, cid);
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
                citations: m.citations ?? [],
                metadata: {
                  ui_mode: 'chat_structured',
                  llm_model: '(history)',
                  latency_ms: 0,
                  confidence: 0,
                  // ADR-027 — 출제 응답이면 카드(그림·①②③④)로 복원.
                  grounding: m.grounding,
                  assessment_items: m.assessment_items,
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
      await deleteConversation(domainId, cid);
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

  // 대화 검색
  const [searchQuery, setSearchQuery] = useState('');
  const filteredConversations = useMemo(() => {
    if (!searchQuery.trim()) return conversations;
    const q = searchQuery.toLowerCase();
    return conversations.filter((c) =>
      (c.title ?? c.conversation_id).toLowerCase().includes(q),
    );
  }, [conversations, searchQuery]);

  // 새 메시지 도착 시 thread 하단으로 자동 scroll
  const threadEndRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    threadEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
  }, [thread, loading]);

  // textarea 자동 높이 + Enter 전송 (Shift+Enter 줄바꿈)
  const onKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
      e.preventDefault();
      void handleSubmit(e as unknown as React.FormEvent);
    }
  };

  return (
    <div className="flex h-screen bg-gray-50 text-gray-900 font-sans antialiased">
      {/* 좌측 — 대화 목록 */}
      <aside className="w-72 border-r border-gray-200 bg-white flex flex-col">
        <div className="p-3 border-b border-gray-200 space-y-2">
          <button
            onClick={handleNewConversation}
            className="w-full px-3 py-2 bg-gray-900 text-white rounded-lg text-sm font-medium hover:bg-gray-800 active:bg-gray-700 transition-colors flex items-center justify-center gap-1.5"
          >
            <span className="text-base leading-none">+</span> 새 대화
          </button>
          {conversations.length > 3 && (
            <input
              type="text"
              value={searchQuery}
              onChange={(e) => setSearchQuery(e.target.value)}
              placeholder="대화 검색…"
              className="w-full px-3 py-1.5 text-xs bg-gray-50 border border-gray-200 rounded-lg placeholder:text-gray-400 focus:outline-none focus:border-gray-400 focus:bg-white"
            />
          )}
        </div>

        {(me?.is_admin || me?.is_auditor || me?.is_platform_admin) && (
          <div className="px-3 py-2 border-b border-gray-200">
            <Link
              href="/console"
              className="text-[11px] text-gray-500 hover:text-gray-900 font-medium"
            >
              관리자 콘솔 →
            </Link>
          </div>
        )}

        <div className="flex-1 overflow-y-auto px-2 py-2 space-y-0.5">
          {filteredConversations.length === 0 && (
            <p className="text-xs text-gray-400 px-3 py-6 text-center">
              {searchQuery ? '일치하는 대화 없음' : '아직 대화 없음'}
            </p>
          )}
          {filteredConversations.map((c) => {
            const active = currentConversationId === c.conversation_id;
            return (
              <div
                key={c.conversation_id}
                className={`group relative px-3 py-2 rounded-lg text-sm cursor-pointer transition-colors ${
                  active
                    ? 'bg-gray-100 ring-1 ring-gray-200'
                    : 'hover:bg-gray-50'
                }`}
                onClick={() => handleSelectConversation(c.conversation_id)}
              >
                <div className="truncate font-medium pr-6 text-gray-900">
                  {c.title || '새 대화'}
                </div>
                <div className="text-[11px] text-gray-500 mt-0.5 flex items-center gap-1.5">
                  <span>
                    {new Date(c.updated_at).toLocaleDateString('ko-KR', {
                      month: 'numeric',
                      day: 'numeric',
                    })}
                  </span>
                  <span className="text-gray-300">·</span>
                  <span>{c.message_count}건</span>
                </div>
                <button
                  onClick={(e) => {
                    e.stopPropagation();
                    void handleDeleteConversation(c.conversation_id);
                  }}
                  className="absolute right-1.5 top-2 opacity-0 group-hover:opacity-100 text-gray-400 hover:text-red-600 text-xs w-5 h-5 rounded flex items-center justify-center hover:bg-white transition-all"
                  title="삭제"
                >
                  ✕
                </button>
              </div>
            );
          })}
        </div>

        {/* 사이드바 하단: user dropdown */}
        {me && (
          <div className="p-3 border-t border-gray-200">
            <Link
              href="/account"
              className="flex items-center gap-2.5 px-2 py-1.5 rounded-lg hover:bg-gray-100 transition-colors group"
            >
              <div className="w-8 h-8 rounded-full bg-gray-900 text-white flex items-center justify-center text-xs font-semibold flex-shrink-0">
                {(me.preferred_username ?? me.email ?? me.user_id)
                  .charAt(0)
                  .toUpperCase()}
              </div>
              <div className="min-w-0 flex-1">
                <div className="truncate text-xs font-medium text-gray-900">
                  {me.preferred_username ?? me.email ?? me.user_id}
                </div>
                <div className="truncate text-[10px] text-gray-500">
                  {me.domain_id}
                </div>
              </div>
              <span className="text-gray-400 text-xs group-hover:text-gray-700">›</span>
            </Link>
            <button
              onClick={async () => {
                const url = await logout(domainId);
                window.location.href = url || '/';
              }}
              className="w-full mt-1 px-2 py-1.5 text-[11px] text-gray-500 hover:bg-gray-100 hover:text-gray-700 rounded-lg text-left"
            >
              로그아웃
            </button>
          </div>
        )}
      </aside>

      {/* 중앙 — 채팅 영역 */}
      <main className="flex-1 flex flex-col bg-white min-w-0">
        <header className="px-6 py-3 border-b border-gray-200 flex justify-between items-center bg-white/95 backdrop-blur-sm sticky top-0 z-10">
          <div className="flex items-center gap-2 min-w-0">
            <h1 className="font-semibold text-sm text-gray-900 truncate">
              {currentTitle ?? '새 대화'}
            </h1>
            <span className="text-gray-300 hidden sm:inline">·</span>
            <DomainSwitcher domainId={domainId} section="chat" />
          </div>
        </header>

        <div className="flex-1 overflow-y-auto">
          <div className="max-w-3xl mx-auto px-4 py-8 space-y-6">
            {thread.length === 0 && !loading && (
              <div className="text-center mt-24 animate-fade-in">
                <div className="w-12 h-12 mx-auto rounded-2xl bg-gradient-to-br from-brand-500 to-brand-700 flex items-center justify-center text-white text-2xl mb-4">
                  ✦
                </div>
                <p className="text-xl font-semibold text-gray-900">
                  무엇을 도와드릴까요?
                </p>
                <p className="text-sm mt-2 text-gray-500">
                  질문을 입력해 대화를 시작하세요
                </p>
              </div>
            )}
            {thread.map((item, i) =>
              item.role === 'user' ? (
                <div key={i} className="flex justify-end animate-slide-up">
                  <div className="max-w-[75%] bg-gray-900 text-white rounded-2xl rounded-br-md px-4 py-2.5 whitespace-pre-wrap break-words text-sm leading-relaxed shadow-sm">
                    {item.content}
                  </div>
                </div>
              ) : (
                <div key={i} className="flex justify-start animate-slide-up">
                  <div className="max-w-full w-full">
                    <AnswerCard
                      response={item.response}
                      domainId={domainId}
                      onCitationClick={setSelectedCitation}
                    />
                  </div>
                </div>
              ),
            )}
            {loading && (
              <div className="flex justify-start animate-fade-in">
                <div className="bg-gray-50 border border-gray-200 rounded-2xl px-4 py-3 flex items-center gap-1">
                  <span className="inline-block w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce [animation-delay:-0.3s]" />
                  <span className="inline-block w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce [animation-delay:-0.15s]" />
                  <span className="inline-block w-1.5 h-1.5 bg-gray-400 rounded-full animate-bounce" />
                </div>
              </div>
            )}
            {error && (
              <div className="bg-red-50 border border-red-200 text-red-700 text-sm rounded-xl px-4 py-3 animate-slide-up">
                <span className="font-medium">오류</span> · {error}
              </div>
            )}
            <div ref={threadEndRef} />
          </div>
        </div>

        <form
          onSubmit={handleSubmit}
          className="border-t border-gray-200 bg-white px-4 py-4 sticky bottom-0"
        >
          <div className="max-w-3xl mx-auto">
            <div className="relative flex items-end gap-2 rounded-2xl border border-gray-300 bg-white px-3 py-2 focus-within:border-gray-900 focus-within:ring-1 focus-within:ring-gray-900 transition-all">
              <textarea
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                onKeyDown={onKeyDown}
                placeholder="질문을 입력하세요"
                rows={1}
                className="flex-1 bg-transparent resize-none focus:outline-none text-sm leading-relaxed py-1.5 max-h-40 placeholder:text-gray-400"
                style={{ minHeight: '24px' }}
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
                className="flex-shrink-0 w-8 h-8 bg-gray-900 text-white rounded-lg disabled:bg-gray-200 disabled:text-gray-400 disabled:cursor-not-allowed hover:bg-gray-800 transition-colors flex items-center justify-center"
                title="전송 (Enter)"
              >
                ↑
              </button>
            </div>
            <p className="text-[10px] text-gray-400 mt-2 px-1 text-center">
              <kbd className="px-1 py-0.5 bg-gray-100 border border-gray-200 rounded text-[10px]">
                Enter
              </kbd>{' '}
              전송 ·{' '}
              <kbd className="px-1 py-0.5 bg-gray-100 border border-gray-200 rounded text-[10px]">
                Shift+Enter
              </kbd>{' '}
              줄바꿈
            </p>
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
