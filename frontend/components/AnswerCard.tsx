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
import type {
  ChatAssessmentItem,
  ChatResponse,
  Citation,
  Grounding,
} from '@/lib/types';

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

  // ADR-027 — 출제 응답은 본문 markdown 대신 문항 카드로 렌더(보기 중복·정답 노출 정리).
  const showAssessmentCard =
    grounding === 'assessment' &&
    (response.metadata.assessment_items?.length ?? 0) > 0;

  return (
    <div className="bg-gray-50 border border-gray-200 rounded-2xl rounded-bl-md px-5 py-4 my-1 text-sm shadow-sm">
      {grounding && (
        <div className="mb-2">
          {grounding === 'grounded' ? (
            <Badge tone="info" title="등록된 문서에서 근거를 찾아 인용했습니다.">
              📑 문서 근거
            </Badge>
          ) : grounding === 'assessment' ? (
            <Badge
              tone="info"
              title="문제은행의 승인 문항을 근거로 새 문제를 출제했습니다. 생성 문항은 저장되지 않는 일회성입니다."
            >
              📝 출제 · 문제은행 근거
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

      {showAssessmentCard ? (
        <AssessmentItems items={response.metadata.assessment_items ?? []} />
      ) : (
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
      )}

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

// ADR-027 — 보기 라벨 통일(①②③④). LLM이 보기에 라벨을 빼먹거나 제각각(가./a./1.)
// 붙이는 것을 한 형식으로 정규화한다.
const CIRCLED = ['①', '②', '③', '④', '⑤', '⑥', '⑦', '⑧', '⑨', '⑩'];

// 기존 선두 라벨(①-⑳ / 가)·나. / A)·a. / 1)·1.)을 떼어 내용만 남긴다.
// "1NF (First Normal Form)" 처럼 구분자 없는 내용은 보존(숫자 뒤 . ) 공백을 요구).
function stripOptionLabel(s: string): string {
  return (s ?? '')
    .replace(/^\s*(?:[①-⑳]\s*|(?:[가-힣]|[A-Za-z]|\d{1,2})[.)]\s+)/, '')
    .trim();
}

function circledFor(i: number): string {
  return CIRCLED[i] ?? `${i + 1}.`;
}

// 정답 문자열을 보기 index로 해석 — 라벨(A/가/1/①) 또는 보기 본문 매칭. 실패 시 -1.
function resolveAnswerIndex(answer: string, strippedChoices: string[]): number {
  const a = (answer ?? '').trim();
  if (!a) return -1;
  const al = stripOptionLabel(a).toLowerCase();
  const lowered = strippedChoices.map((c) => c.toLowerCase());
  let idx = lowered.findIndex((c) => c === al);
  if (idx >= 0) return idx;
  const m = a.match(/^\s*([A-Ja-j가나다라마바사①-⑩]|\d{1,2})/);
  if (m) {
    const ch = m[1];
    if (/^[A-J]$/.test(ch)) return ch.charCodeAt(0) - 65;
    if (/^[a-j]$/.test(ch)) return ch.charCodeAt(0) - 97;
    if (/^\d{1,2}$/.test(ch)) return Number(ch) - 1;
    const ko = '가나다라마바사'.indexOf(ch);
    if (ko >= 0) return ko;
    const ci = CIRCLED.indexOf(ch);
    if (ci >= 0) return ci;
  }
  idx = lowered.findIndex((c) => al && (c.includes(al) || al.includes(c)));
  return idx;
}

/**
 * ADR-027 — 채팅 출제 문항 카드. 보기를 ①②③④로 통일 렌더하고,
 * 정답·해설은 펼침(details)으로 숨겨 먼저 풀어볼 수 있게 한다.
 */
function AssessmentItems({ items }: { items: ChatAssessmentItem[] }) {
  return (
    <div className="space-y-3 my-1">
      {items.map((it, i) => {
        const choices = (it.choices ?? []).map(stripOptionLabel);
        const ansIdx = resolveAnswerIndex(it.answer, choices);
        return (
          <div
            key={i}
            className="bg-white border border-gray-200 rounded-xl px-4 py-3 text-sm"
          >
            <p className="font-medium text-gray-900">
              {i + 1}.{' '}
              {it.subject && (
                <span className="text-[11px] text-gray-400 mr-1">[{it.subject}]</span>
              )}
              {it.question_text}
            </p>
            {it.image_url && (
              // ADR-027 — 그림 문제: 문제은행의 기존 그림을 표시(figure-reuse).
              // eslint-disable-next-line @next/next/no-img-element
              <img
                src={it.image_url}
                alt="문제 그림"
                className="mt-2 max-h-72 w-auto rounded-lg border border-gray-200"
              />
            )}
            <ul className="mt-2 space-y-1 text-gray-700">
              {choices.map((c, ci) => (
                <li key={ci}>
                  <span className="text-gray-500 mr-1">{circledFor(ci)}</span>
                  {c}
                </li>
              ))}
            </ul>
            <details className="mt-2 group">
              <summary className="cursor-pointer text-[12px] text-brand-600 hover:underline select-none">
                정답 · 해설 보기
              </summary>
              <div className="mt-1 text-[13px] text-gray-700">
                <span className="font-semibold text-green-700">
                  정답:{' '}
                  {ansIdx >= 0
                    ? `${circledFor(ansIdx)} ${choices[ansIdx]}`
                    : it.answer}
                </span>
                {it.explanation && (
                  <p className="mt-0.5 text-gray-600">{it.explanation}</p>
                )}
              </div>
            </details>
          </div>
        );
      })}
      <p className="text-[11px] text-gray-400">
        문제은행을 근거로 생성한 일회성 문항입니다. 저장되지 않습니다.
      </p>
    </div>
  );
}
