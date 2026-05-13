/**
 * AnswerCard — ADR-016 §2.2.
 * 4-type marker 분리 표기, support_level 시각화, unsupported ⚠ 인라인.
 */

import type { ChatResponse, Citation } from '@/lib/types';

interface Props {
  response: ChatResponse;
  onCitationClick: (c: Citation) => void;
}

export default function AnswerCard({ response, onCitationClick }: Props) {
  if (response.status === 'fallback') {
    return (
      <div className="bg-gray-100 border border-gray-300 rounded p-4 my-4">
        <p className="text-gray-700">{response.answer}</p>
        <p className="text-xs text-gray-500 mt-2">
          fallback_reason: {response.fallback.reason}
        </p>
        {response.fallback.near_misses.length > 0 && (
          <div className="mt-3">
            <p className="text-sm font-bold">근접한 후보 (참고용, 직접 인용 아님):</p>
            <ul className="text-sm">
              {response.fallback.near_misses.map((m, i) => (
                <li key={i}>
                  - {m.title} {m.page_number ? `p.${m.page_number}` : ''}{' '}
                  {m.section_title ? `§${m.section_title}` : ''} (관련도{' '}
                  {m.rerank_score.toFixed(2)})
                </li>
              ))}
            </ul>
          </div>
        )}
        {response.fallback.suggested_actions.length > 0 && (
          <div className="mt-3">
            <p className="text-sm font-bold">다음 시도해 보세요:</p>
            <ul className="text-sm list-disc pl-5">
              {response.fallback.suggested_actions.map((a, i) => (
                <li key={i}>{a}</li>
              ))}
            </ul>
          </div>
        )}
      </div>
    );
  }

  return (
    <div className="bg-white border border-gray-200 rounded p-4 my-4 shadow-sm">
      <div className="prose prose-sm max-w-none">
        {response.answer_segments.map((seg, i) => (
          <span key={i}>
            {seg.text}
            {seg.citations.length > 0 &&
              seg.citations.map((cIdx) => {
                const cit = response.citations.find((c) => c.marker === `[${cIdx}]`);
                if (!cit) return null;
                const colorClass =
                  cit.support_type === 'direct'
                    ? 'text-citation-direct'
                    : cit.support_type === 'synthesis'
                      ? 'text-citation-synthesis'
                      : cit.support_type === 'inference'
                        ? 'text-citation-inference'
                        : 'text-citation-conflict';
                return (
                  <button
                    key={cIdx}
                    onClick={() => onCitationClick(cit)}
                    className={`${colorClass} font-bold mx-0.5 hover:underline`}
                    title={
                      cit.support_level === 'medium'
                        ? '⚠ 의미 유사도 일부 약함'
                        : undefined
                    }
                  >
                    [{cIdx}]
                  </button>
                );
              })}
            {seg.unsupported && <span className="text-yellow-600 mx-1" title="근거 미확보">⚠</span>}{' '}
          </span>
        ))}
      </div>

      <div className="mt-4 pt-3 border-t border-gray-100 text-xs text-gray-500">
        <span>모델: {response.metadata.llm_model}</span>
        {' · '}
        <span>응답 시간: {(response.metadata.latency_ms / 1000).toFixed(2)}s</span>
        {' · '}
        <span>confidence: {response.metadata.confidence.toFixed(2)}</span>
      </div>

      <div className="mt-2 flex gap-2">
        <button className="px-2 py-1 text-xs border rounded">좋아요</button>
        <button className="px-2 py-1 text-xs border rounded">별로예요</button>
        <button className="px-2 py-1 text-xs border rounded">복사</button>
      </div>
    </div>
  );
}
