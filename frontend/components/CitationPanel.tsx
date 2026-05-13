/**
 * CitationPanel — ADR-016 §2.3.
 * support_level 배지, claim_text, 원문 발췌, 메타데이터.
 */

import type { Citation } from '@/lib/types';

interface Props {
  citation: Citation | null;
}

export default function CitationPanel({ citation }: Props) {
  if (!citation) {
    return (
      <div className="text-gray-400 text-sm">
        답변의 [숫자] 마커를 클릭하면 근거가 여기에 표시됩니다.
      </div>
    );
  }

  const badgeColor =
    citation.support_level === 'strong'
      ? 'bg-support-strong text-white'
      : citation.support_level === 'medium'
        ? 'bg-support-medium text-white'
        : 'bg-gray-400 text-white';

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-2">
        <span className="font-bold text-lg">{citation.marker}</span>
        <span className={`px-2 py-0.5 rounded text-xs ${badgeColor}`}>
          {citation.support_level} · {citation.support_type}
        </span>
        {citation.support_level === 'medium' && (
          <span className="text-yellow-600 text-sm" title="의미 유사도 일부 약함">
            ⚠
          </span>
        )}
      </div>

      <div>
        <p className="font-bold">{citation.title}</p>
        {citation.page_number && (
          <p className="text-sm text-gray-600">p.{citation.page_number}</p>
        )}
        {citation.section_title && (
          <p className="text-sm text-gray-600">§{citation.section_title}</p>
        )}
      </div>

      <div className="bg-gray-50 rounded p-3 text-sm">
        <p className="font-bold text-xs text-gray-500 mb-1">claim_text</p>
        <p>{citation.claim_text}</p>
      </div>

      <div className="bg-blue-50 rounded p-3 text-sm">
        <p className="font-bold text-xs text-gray-500 mb-1">원문 발췌</p>
        <p>{citation.excerpt}</p>
      </div>

      <div className="text-xs text-gray-500 border-t pt-2">
        <p>관련도: {citation.score.toFixed(2)}</p>
        <p>rerank: {citation.rerank_score.toFixed(2)}</p>
        <p>chunk_id: <code className="text-xs">{citation.chunk_id}</code></p>
      </div>

      {citation.support_type === 'inference' && citation.caveat && (
        <div className="bg-orange-50 rounded p-3 text-sm border border-orange-200">
          <p className="font-bold text-orange-700 mb-1">🔍 추론 caveat</p>
          <p>{citation.caveat}</p>
        </div>
      )}

      <button className="w-full mt-2 px-3 py-2 border rounded text-sm">원문 열기</button>
    </div>
  );
}
