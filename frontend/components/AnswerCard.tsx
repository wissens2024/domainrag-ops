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
import ConflictBox from './ConflictBox';
import InferenceJudgePopover from './InferenceJudgePopover';
import { postFeedback } from '@/lib/api';
import type { ChatResponse, Citation } from '@/lib/types';

interface Props {
  response: ChatResponse;
  tenantId: string;
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

export default function AnswerCard({ response, tenantId, onCitationClick }: Props) {
  const [feedbackSent, setFeedbackSent] = useState<'good' | 'bad' | null>(null);
  const [feedbackError, setFeedbackError] = useState<string | null>(null);

  const sendFeedback = async (verdict: 'good' | 'bad') => {
    if (response.status !== 'success' && response.status !== 'fallback') return;
    setFeedbackError(null);
    try {
      await postFeedback(tenantId, {
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

  return (
    <div className="bg-white border border-gray-200 rounded p-4 my-4 shadow-sm">
      <div className="prose prose-sm max-w-none">
        {answerSegments.map((seg, i) => (
          <span key={i}>
            {seg.text}
            {(seg.citations ?? []).length > 0 &&
              (seg.citations ?? []).map((cIdx) => {
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
        ))}
      </div>

      {conflictCitations.map((c) => (
        <ConflictBox
          key={c.citation_id}
          conflictCitation={c}
          allCitations={response.citations}
        />
      ))}

      <div className="mt-4 pt-3 border-t border-gray-100 text-xs text-gray-500 flex flex-wrap gap-x-3">
        <span>모델: {response.metadata.llm_model}</span>
        <span>응답 시간: {(response.metadata.latency_ms / 1000).toFixed(2)}s</span>
        <span>confidence: {response.metadata.confidence.toFixed(2)}</span>
        {response.metadata.lora_adapter && (
          <span>LoRA: {response.metadata.lora_adapter}</span>
        )}
        <span>mode: {response.metadata.ui_mode}</span>
      </div>

      <div className="mt-2 flex gap-2 items-center">
        <button
          onClick={() => sendFeedback('good')}
          disabled={feedbackSent !== null}
          className={`px-2 py-1 text-xs border rounded ${
            feedbackSent === 'good' ? 'bg-green-100 border-green-300' : ''
          }`}
        >
          👍 좋아요
        </button>
        <button
          onClick={() => sendFeedback('bad')}
          disabled={feedbackSent !== null}
          className={`px-2 py-1 text-xs border rounded ${
            feedbackSent === 'bad' ? 'bg-red-100 border-red-300' : ''
          }`}
        >
          👎 별로예요
        </button>
        <button onClick={copyToClipboard} className="px-2 py-1 text-xs border rounded">
          📋 복사
        </button>
        {feedbackError && (
          <span className="text-xs text-red-600">{feedbackError}</span>
        )}
        {feedbackSent && !feedbackError && (
          <span className="text-xs text-gray-500">피드백 감사합니다.</span>
        )}
      </div>
    </div>
  );
}
