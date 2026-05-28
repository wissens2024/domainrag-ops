/**
 * Quality Review Queue — /{tid}/admin/assessment/review-queue (ADR-014 §5 + Y2).
 *
 * quality_status='draft' 또는 'reviewed' items 목록 + approve 직행.
 */
'use client';

import { useParams } from 'next/navigation';
import { useState } from 'react';
import useSWR, { mutate } from 'swr';
import {
  approveAssessmentItem,
  getReviewQueue,
} from '@/lib/api';
import type { AssessmentListResult } from '@/lib/types';

export default function ReviewQueuePage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const [page, setPage] = useState(1);
  const pageSize = 20;
  const swrKey = `assessment-review:${domainId}:${page}`;
  const { data, isLoading } = useSWR<AssessmentListResult>(
    domainId ? swrKey : null,
    () => getReviewQueue(domainId, { page, page_size: pageSize }),
  );

  const handleApprove = async (itemId: string) => {
    try {
      await approveAssessmentItem(domainId, itemId);
      void mutate(swrKey);
    } catch (e) {
      alert(`승인 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">Quality Review Queue</h1>
      <p className="text-sm text-gray-500 mb-4">
        draft / reviewed 상태 items만. validator 결과 + score 기반으로 운영자가 최종 검토.
      </p>

      {isLoading && <p>로딩...</p>}
      {data && (
        <>
          <ul className="space-y-3">
            {data.items.map((it) => (
              <li key={it.item_id} className="border rounded p-4">
                <div className="flex justify-between items-start mb-2">
                  <span className="font-mono text-xs text-gray-500">{it.item_id}</span>
                  <span className="text-xs">
                    status: <b>{it.quality_status}</b> · score:{' '}
                    {it.quality_score?.toFixed(2) || '-'}
                  </span>
                </div>
                <div className="font-medium mb-2">{it.question_text}</div>
                <ul className="text-sm text-gray-700 space-y-0.5 mb-2">
                  {it.choices.map((c, ci) => (
                    <li key={ci}>
                      {String.fromCharCode(65 + ci)}. {c}{' '}
                      {c === it.answer && <span className="text-green-600">✓</span>}
                    </li>
                  ))}
                </ul>
                {it.explanation && (
                  <p className="text-xs text-gray-500 mb-2">해설: {it.explanation}</p>
                )}
                {Object.keys(it.validator_results || {}).length > 0 && (
                  <details>
                    <summary className="cursor-pointer text-xs text-gray-500">
                      validator_results
                    </summary>
                    <pre className="text-xs bg-gray-50 p-2 mt-1">
                      {JSON.stringify(it.validator_results, null, 2)}
                    </pre>
                  </details>
                )}
                <div className="mt-3">
                  <button
                    onClick={() => void handleApprove(it.item_id)}
                    className="px-3 py-1 bg-green-600 text-white rounded text-sm"
                  >
                    ✓ approve
                  </button>
                </div>
              </li>
            ))}
            {data.items.length === 0 && (
              <li className="text-center text-gray-500">대기 중인 item이 없습니다.</li>
            )}
          </ul>

          <div className="mt-4 flex justify-between text-sm">
            <span>총 {data.total}건</span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage(page - 1)}
                className="px-2 py-1 border rounded disabled:bg-gray-100"
              >
                이전
              </button>
              <button
                disabled={page * pageSize >= data.total}
                onClick={() => setPage(page + 1)}
                className="px-2 py-1 border rounded disabled:bg-gray-100"
              >
                다음
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
