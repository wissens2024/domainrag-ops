/**
 * Indexing Jobs — /{tid}/admin/indexing (ADR-016 §3.3 + ADR-017 §7).
 *
 * 잡 목록 + 상태 필터 + 진행률 + failed chunks 표시 + retry.
 */
'use client';

import { useParams } from 'next/navigation';
import { useState } from 'react';
import useSWR, { mutate } from 'swr';
import { listIndexingJobs, retryIndexingJob } from '@/lib/api';
import type {
  IndexingJob,
  IndexingJobListResult,
  IndexingStatus,
} from '@/lib/types';

const STATUS_COLOR: Record<IndexingStatus, string> = {
  pending: 'bg-gray-100 text-gray-700',
  parsing: 'bg-blue-100 text-blue-700',
  chunking: 'bg-blue-100 text-blue-700',
  embedding: 'bg-blue-100 text-blue-700',
  indexing: 'bg-blue-100 text-blue-700',
  completed: 'bg-green-100 text-green-700',
  partial: 'bg-yellow-100 text-yellow-700',
  failed: 'bg-red-100 text-red-700',
};

const STATUS_OPTIONS: IndexingStatus[] = [
  'pending',
  'parsing',
  'chunking',
  'embedding',
  'indexing',
  'completed',
  'partial',
  'failed',
];

export default function IndexingPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const [statusFilter, setStatusFilter] = useState<IndexingStatus | ''>('');
  const [page, setPage] = useState(1);
  const pageSize = 20;
  const [expanded, setExpanded] = useState<string | null>(null);

  const swrKey = `indexing:${domainId}:${statusFilter}:${page}:${pageSize}`;
  const { data, isLoading, error } = useSWR<IndexingJobListResult>(
    domainId ? swrKey : null,
    () =>
      listIndexingJobs(domainId, {
        page,
        page_size: pageSize,
        status: statusFilter || undefined,
      }),
    { refreshInterval: 5000 },
  );

  const handleRetry = async (job: IndexingJob) => {
    if (!confirm(`job ${job.job_id} retry?`)) return;
    try {
      await retryIndexingJob(domainId, job.job_id);
      void mutate(swrKey);
    } catch (e) {
      alert(`retry 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">인덱싱 모니터링</h1>

      <div className="flex gap-2 mb-4">
        <select
          value={statusFilter}
          onChange={(e) => {
            setStatusFilter(e.target.value as IndexingStatus | '');
            setPage(1);
          }}
          className="px-3 py-2 border rounded text-sm"
        >
          <option value="">모든 상태</option>
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      {isLoading && <p>로딩 중...</p>}
      {error && <p className="text-red-600">로드 실패: {error.message}</p>}

      {data && (
        <>
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b bg-gray-50 text-left">
                <th className="p-2">job_id / doc_id</th>
                <th className="p-2">mode</th>
                <th className="p-2">상태</th>
                <th className="p-2">진행률</th>
                <th className="p-2">indexed / failed</th>
                <th className="p-2">시작</th>
                <th className="p-2">액션</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((j) => (
                <>
                  <tr key={j.job_id} className="border-b hover:bg-gray-50">
                    <td className="p-2">
                      <div className="font-mono text-xs">{j.job_id.slice(0, 12)}…</div>
                      <div className="text-xs text-gray-500">
                        {j.doc_id} v{j.doc_version}
                      </div>
                    </td>
                    <td className="p-2 text-xs">{j.mode}</td>
                    <td className="p-2">
                      <span
                        className={`px-2 py-0.5 rounded text-xs ${STATUS_COLOR[j.status]}`}
                      >
                        {j.status}
                      </span>
                    </td>
                    <td className="p-2">
                      <div className="h-2 bg-gray-100 rounded w-24 overflow-hidden">
                        <div
                          className="h-full bg-blue-500"
                          style={{ width: `${Math.round(j.progress * 100)}%` }}
                        />
                      </div>
                      <div className="text-xs text-gray-500">
                        {Math.round(j.progress * 100)}%
                      </div>
                    </td>
                    <td className="p-2 text-xs">
                      {j.indexed_chunks} / {j.failed_chunks.length}
                    </td>
                    <td className="p-2 text-xs">
                      {j.started_at
                        ? new Date(j.started_at).toLocaleString('ko-KR')
                        : '-'}
                    </td>
                    <td className="p-2 text-xs space-x-1">
                      <button
                        className="px-1 border rounded"
                        onClick={() =>
                          setExpanded(expanded === j.job_id ? null : j.job_id)
                        }
                      >
                        {expanded === j.job_id ? '접기' : '상세'}
                      </button>
                      {(j.status === 'failed' || j.status === 'partial') && (
                        <button
                          className="px-1 border rounded"
                          onClick={() => void handleRetry(j)}
                        >
                          retry
                        </button>
                      )}
                    </td>
                  </tr>
                  {expanded === j.job_id && (
                    <tr key={`${j.job_id}-detail`} className="bg-gray-50">
                      <td colSpan={7} className="p-3 text-xs">
                        {j.error_message && (
                          <div className="text-red-600 mb-2">
                            error: {j.error_message}
                          </div>
                        )}
                        {j.failure_rate !== null && (
                          <div>failure_rate: {j.failure_rate.toFixed(2)}</div>
                        )}
                        {j.failed_chunks.length > 0 && (
                          <details className="mt-2">
                            <summary>
                              failed_chunks ({j.failed_chunks.length})
                            </summary>
                            <pre className="text-xs mt-1 max-h-40 overflow-y-auto">
                              {JSON.stringify(j.failed_chunks, null, 2)}
                            </pre>
                          </details>
                        )}
                      </td>
                    </tr>
                  )}
                </>
              ))}
              {data.items.length === 0 && (
                <tr>
                  <td colSpan={7} className="p-4 text-center text-gray-500">
                    조건에 맞는 잡이 없습니다.
                  </td>
                </tr>
              )}
            </tbody>
          </table>

          <div className="mt-4 flex justify-between items-center text-sm">
            <span>
              총 {data.total}건 · {page} / {Math.max(1, Math.ceil(data.total / pageSize))}
            </span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage(page - 1)}
                className="px-2 py-1 border rounded disabled:bg-gray-100 disabled:text-gray-400"
              >
                이전
              </button>
              <button
                disabled={page * pageSize >= data.total}
                onClick={() => setPage(page + 1)}
                className="px-2 py-1 border rounded disabled:bg-gray-100 disabled:text-gray-400"
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
