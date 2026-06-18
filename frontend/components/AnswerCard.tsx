/**
 * AnswerCard — ADR-016 §2.2.
 * 4-type marker 분리 표기, support_level 시각화, unsupported ⚠ 인라인.
 *
 * 4-type 색상 매핑 (tailwind.config.js):
 *   direct    → text-citation-direct (blue)
 *   synthesis → text-citation-synthesis (purple)
 *   inference → text-citation-inference (orange, 🔍 popover)
 *   conflict  → text-citation-conflict (rose)
 */
'use client';

import { useState } from 'react';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import ConflictBox from './ConflictBox';
import InferenceJudgePopover from './InferenceJudgePopover';
import Badge from './ui/Badge';
import { postFeedback } from '@/lib/api';
import type { ChatResponse, Citation, Grounding } from '@/lib/types';

interface Props {
  response: ChatResponse;
  domainId: string;
  onCitationClick: (c: Citation) => void;
}

function citationColorClass(type: Citation['support_type']): string {
  switch (type) {
    case 'direct':
      return 'text-citation-direct';
    case 'synthesis':
      return 'text-citation-synthesis';
    case 'inference':
      return 'text-citation-inference';
    case 'conflict':
      return 'text-citation-conflict';
    default:
      return 'text-blue-600';
  }
}

export default function AnswerCard({ response, domainId, onCitationClick }: Props) {
  const [feedbackSent, setFeedbackSent] = useState<'good' | 'bad' | null>(null);
  const [feedbackError, setFeedbackError] = useState<string | null>(null);

  const sendFeedback = async (verdict: 'good' | 'bad') => {
    if (response.status !== 'success' && response.status !== 'fallback') return;
    setFeedbackError(null);
    try {
      await postFeedback(domainId, {
        message_id: response.message_id,
        feedback: verdict,
      });
      setFeedbackSent(verdict);
    } catch (e) {
      setFeedbackError(e instanceof Error ? e.message : 'feedback_failed');
    }
  };

  const copyToClipboard = () => {
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      navigator.clipboard.writeText(response.answer).catch(() => undefined);
    }
  };

  if (response.status === 'fallback') {
    const nearMisses = response.fallback?.near_misses ?? [];
    const suggestedActions = response.fallback?.suggested_actions ?? [];
    return (
      <div className="bg-gray-100 border border-gray-300 rounded p-4 my-4">
        <p className="text-gray-700">{response.answer}</p>
        <p className="text-xs text-gray-500 mt-2">
          fallback_reason: {response.fallback?.reason ?? 'unknown'}
        </p>
        {nearMisses.length > 0 && (
          <div className="mt-3">
            <p className="text-sm font-bold">근접한 후보 (참고용, 직접 인용 아님):</p>
            <ul className="text-sm">
              {nearMisses.map((m, i) => (
                <li key={i}>
                  - {m.title} {m.page_number ? `p.${m.page_number}` : ''}{' '}
                  {m.section_title ? `§${m.section_title}` : ''} (관련도{' '}
                  {m.rerank_score.toFixed(2)})
                </li>
              ))}
            </ul>
          </div>
        )}
        {suggestedActions.length > 0 && (
          <div className="mt-3">
            <p className="text-sm font-bold">다음 시도해 보세요:</p>
            <ul className="text-sm list-disc pl-5">
              {suggestedActions.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    );
  }

  const citations = response.citations ?? [];
  const answerSegments = response.answer_segments ?? [{ text: response.answer ?? '', citations: [] }];
  const conflictCitations = citations.filter((c) => c.support_type === 'conflict');

  // citation marker가 있는 segment는 inline node로, 없는 경우는 markdown으로.
  // 일반 대화(ungrounded)에는 citations[]이 비어 markdown 전용 렌더 사용.
  const hasCitations = citations.length > 0;

  // ADR-023 §4 — 근거 유무를 UI로 명확히 구분. 시스템이 근거 여부를 판단·표시한다
  // (사용자가 추측하지 않는다). 과거 대화 복원처럼 grounding 미상이면 배지를 숨긴다.
  const grounding: Grounding | undefined =
    response.metadata.grounding ?? (hasCitations ? 'grounded' : undefined);

  return (
    <div className="bg-gray-50 border border-gray-200 rounded-2xl rounded-bl-md px-5 py-4 my-1 text-sm shadow-sm">
      {grounding && (
        <div className="mb-2">
          {grounding === 'grounded' ? (
            <Badge tone="info" title="등록된 문서에서 근거를 찾아 인용했습니다.">
              📑 문서 근거
            </Badge>
          ) : (
            <Badge
              tone="warn"
              title="등록된 문서에서 근거를 찾지 못해 일반 지식으로 답했습니다. 도메인 사실은 담당 부서 확인을 권장합니다."
            >
              💬 일반 대화
            </Badge>
          )}
        </div>
      )}
      <div className="prose prose-sm max-w-none prose-headings:font-semibold prose-headings:text-gray-900 prose-p:my-2 prose-p:leading-relaxed prose-pre:bg-gray-900 prose-pre:text-gray-100 prose-pre:text-xs prose-code:before:hidden prose-code:after:hidden prose-code:bg-gray-100 prose-code:text-gray-800 prose-code:font-normal prose-code:px-1.5 prose-code:py-0.5 prose-code:rounded prose-code:text-[12px] prose-a:text-brand-600 prose-a:no-underline hover:prose-a:underline prose-strong:text-gray-900">
        {!hasCitations ? (
          <ReactMarkdown remarkPlugins={[remarkGfm]}>
            {response.answer || ''}
          </ReactMarkdown>
        ) : (
          answerSegments.map((seg, i) => (
            <span key={i}>
              {seg.text}
              {(seg.citations ?? []).map((cIdx) => {
                const cit = citations.find((c) => c.marker === `[${cIdx}]`);
                if (!cit) return null;
                const colorClass = citationColorClass(cit.support_type);
                const button = (
                  <button
                    key={`${i}-${cIdx}`}
                    onClick={() => onCitationClick(cit)}
                    className={`${colorClass} font-bold mx-0.5 hover:underline`}
                    title={
                      cit.support_level === 'medium'
                        ? '⚠ 의미 유사도 일부 약함'
                        : cit.support_type === 'inference'
                          ? '🔍 추론 근거 (호버하면 reasoning)'
                          : undefined
                    }
                  >
                    [{cIdx}]
                    {cit.support_type === 'inference' && (
                      <span className="ml-0.5">🔍</span>
                    )}
                  </button>
                );
                if (cit.support_type === 'inference') {
                  return (
                    <InferenceJudgePopover key={`p-${i}-${cIdx}`} citation={cit}>
                      {button}
                    </InferenceJudgePopover>
                  );
                }
                return button;
              })}
              {seg.unsupported && (
                <span className="text-yellow-600 mx-1" title="근거 미확보">
                  ⚠
                </span>
              )}{' '}
            </span>
          ))
        )}
      </div>

      {conflictCitations.map((c) => (
        <ConflictBox
          key={c.citation_id}
          conflictCitation={c}
          allCitations={response.citations}
        />
      ))}

      <div className="mt-3 pt-2 border-t border-gray-200 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-gray-500">
        {/* 모델·confidence·LoRA는 사용자에겐 디버그 정보 — 관리자 뷰(Citation Inspector·
            chat_logs)에만 노출. 사용자에겐 응답 시간만. (ADR-016 user/admin 분리) */}
        {response.metadata.latency_ms > 0 && (
          <span>{(response.metadata.latency_ms / 1000).toFixed(2)}s</span>
        )}
        <div className="ml-auto flex items-center gap-1">
          <button
            onClick={copyToClipboard}
            className="px-2 py-1 rounded hover:bg-gray-200 text-gray-500"
            title="답변 복사"
          >
            📋
          </button>
          <button
            onClick={() => sendFeedback('good')}
            disabled={feedbackSent !== null}
            className={`px-2 py-1 rounded hover:bg-gray-200 ${
              feedbackSent === 'good' ? 'bg-green-100 text-green-700' : 'text-gray-500'
            }`}
            title="도움이 됐어요 — 답변 품질 개선에 활용됩니다"
          >
            👍
          </button>
          <button
            onClick={() => sendFeedback('bad')}
            disabled={feedbackSent !== null}
            className={`px-2 py-1 rounded hover:bg-gray-200 ${
              feedbackSent === 'bad' ? 'bg-red-100 text-red-700' : 'text-gray-500'
            }`}
            title="별로예요 — 답변 품질 개선에 활용됩니다"
          >
            👎
          </button>
        </div>
        {feedbackError && (
          <span className="w-full text-red-600">{feedbackError}</span>
        )}
        {feedbackSent && !feedbackError && (
          <span className="w-full text-gray-400">피드백 감사합니다.</span>
        )}
      </div>
    </div>
  );
}
