/**
 * ConflictBox — ADR-010 §7 conflict citation 시각화.
 * support_type='conflict'인 citation의 conflict_groups를 좌우 분리 박스로 표현.
 */
'use client';

import type { Citation } from '@/lib/types';

interface Props {
  conflictCitation: Citation;
  allCitations: Citation[];
}

export default function ConflictBox({ conflictCitation, allCitations }: Props) {
  const groups = conflictCitation.conflict_groups ?? [];
  if (!groups.length) return null;

  return (
    <div className="bg-rose-50 border border-rose-200 rounded p-3 my-3 text-sm">
      <p className="font-bold text-rose-700 mb-2">
        ⚖ 충돌하는 근거가 있습니다 ({conflictCitation.marker})
      </p>
      <p className="text-xs text-rose-600 mb-3">
        {conflictCitation.claim_text}
      </p>
      <div className="grid grid-cols-2 gap-2">
        {groups.slice(0, 2).map((group, gi) => (
          <div
            key={gi}
            className="bg-white border border-rose-100 rounded p-2"
          >
            <p className="text-xs font-bold text-rose-700 mb-1">
              주장 {gi + 1}
            </p>
            <ul className="space-y-1">
              {group.map((cIdx) => {
                const cit = allCitations.find((c) => c.marker === `[${cIdx}]`);
                if (!cit) return null;
                return (
                  <li key={cIdx} className="text-xs">
                    <span className="font-bold">[{cIdx}]</span> {cit.title}
                    {cit.page_number ? ` p.${cit.page_number}` : ''}
                  </li>
                );
              })}
            </ul>
          </div>
        ))}
      </div>
      {conflictCitation.caveat && (
        <p className="text-xs text-rose-700 mt-2 italic">
          ⚠ {conflictCitation.caveat}
        </p>
      )}
    </div>
  );
}
