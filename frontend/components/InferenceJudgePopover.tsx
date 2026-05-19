/**
 * InferenceJudgePopover — ADR-016 §2.4.
 * inference citation의 LLM judge reasoning + caveat을 호버 popover로 표시.
 */
'use client';

import { useState } from 'react';
import type { Citation } from '@/lib/types';

interface Props {
  citation: Citation;
  children: React.ReactNode;
}

export default function InferenceJudgePopover({ citation, children }: Props) {
  const [open, setOpen] = useState(false);

  return (
    <span
      className="relative inline-block"
      onMouseEnter={() => setOpen(true)}
      onMouseLeave={() => setOpen(false)}
    >
      {children}
      {open && (
        <span
          className="absolute z-10 left-0 top-full mt-1 w-80 bg-white border border-orange-300 rounded shadow-lg p-3 text-xs text-gray-800"
          style={{ whiteSpace: 'normal' }}
        >
          <span className="block font-bold text-orange-700 mb-1">
            🔍 추론 근거 ({citation.marker})
          </span>
          <span className="block text-gray-700 mb-2">
            <span className="font-bold">claim:</span> {citation.claim_text}
          </span>
          {citation.inference_chain && (
            <span className="block text-gray-600 mb-2">
              <span className="font-bold">reasoning:</span>{' '}
              {citation.inference_chain}
            </span>
          )}
          {citation.caveat && (
            <span className="block bg-orange-50 rounded p-2 text-orange-800">
              ⚠ {citation.caveat}
            </span>
          )}
        </span>
      )}
    </span>
  );
}
