/**
 * Citation Inspector — /{tid}/admin/citation-inspector (ADR-016 §3.5.1 + ADR-017 §9 + ADR-010 §4).
 *
 * 4-type 분포 + segments 조회 + reverify 액션.
 */
'use client';

import { useParams } from 'next/navigation';
import { useState } from 'react';
import useSWR from 'swr';
import {
  getCitationDistribution,
  getCitationSegments,
  reverifyCitations,
} from '@/lib/api';
import type {
  CitationDistributionResult,
  SupportType,
} from '@/lib/types';

const SUPPORT_TYPES: SupportType[] = ['direct', 'synthesis', 'inference', 'conflict'];

const COLOR_MAP: Record<SupportType, string> = {
  direct: 'bg-citation-direct',
  synthesis: 'bg-citation-synthesis',
  inference: 'bg-citation-inference',
  conflict: 'bg-citation-conflict',
};

export default function CitationInspectorPage() {
  const params = useParams<{ tenantId: string }>();
  const tenantId = params.tenantId;
  const [fromDate, setFromDate] = useState('');
  const [toDate, setToDate] = useState('');
  const [groupBy, setGroupBy] = useState<'day' | 'hour'>('day');
  const [messageId, setMessageId] = useState('');
  const [segments, setSegments] = useState<unknown>(null);
  const [reverifyResult, setReverifyResult] = useState<unknown>(null);

  const swrKey = `citation-distribution:${tenantId}:${fromDate}:${toDate}:${groupBy}`;
  const { data, isLoading, error } = useSWR<CitationDistributionResult>(
    tenantId ? swrKey : null,
    () =>
      getCitationDistribution(tenantId, {
        from_date: fromDate || undefined,
        to_date: toDate || undefined,
        group_by: groupBy,
      }),
  );

  const handleLoadSegments = async () => {
    if (!messageId.trim()) return;
    try {
      const result = await getCitationSegments(tenantId, messageId.trim());
      setSegments(result);
    } catch (e) {
      alert(`로드 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  const handleReverify = async () => {
    if (!confirm('해당 범위의 chat_logs를 재검증합니다. 시간이 걸릴 수 있습니다.')) return;
    try {
      const result = await reverifyCitations(tenantId, {
        from_date: fromDate || undefined,
        to_date: toDate || undefined,
        max_records: 100,
      });
      setReverifyResult(result);
    } catch (e) {
      alert(`재검증 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">Citation Inspector</h1>

      <div className="mb-4 flex gap-2 items-end text-sm">
        <div>
          <label className="block text-xs text-gray-500">from</label>
          <input
            type="date"
            value={fromDate}
            onChange={(e) => setFromDate(e.target.value)}
            className="px-2 py-1 border rounded"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500">to</label>
          <input
            type="date"
            value={toDate}
            onChange={(e) => setToDate(e.target.value)}
            className="px-2 py-1 border rounded"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500">group_by</label>
          <select
            value={groupBy}
            onChange={(e) => setGroupBy(e.target.value as 'day' | 'hour')}
            className="px-2 py-1 border rounded"
          >
            <option value="day">day</option>
            <option value="hour">hour</option>
          </select>
        </div>
        <button
          onClick={handleReverify}
          className="px-3 py-1 border rounded bg-yellow-50 ml-2"
        >
          ⟳ 재검증 (max 100건)
        </button>
      </div>

      {reverifyResult ? (
        <div className="mb-4 p-3 bg-blue-50 rounded text-sm">
          <p className="font-bold">재검증 결과</p>
          <pre className="text-xs mt-1">{JSON.stringify(reverifyResult, null, 2)}</pre>
        </div>
      ) : null}

      <section className="mb-6">
        <h2 className="font-bold mb-2">4-type 분포</h2>
        {isLoading && <p>로딩 중...</p>}
        {error && <p className="text-red-600">로드 실패: {error.message}</p>}
        {data && (
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b bg-gray-50 text-left">
                <th className="p-2">시각 (granularity={data.granularity})</th>
                {SUPPORT_TYPES.map((t) => (
                  <th key={t} className="p-2 capitalize">
                    {t}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody>
              {data.buckets.map((b) => (
                <tr key={b.bucket} className="border-b">
                  <td className="p-2 text-xs">{b.bucket}</td>
                  {SUPPORT_TYPES.map((t) => {
                    const v = b.counts[t] || 0;
                    return (
                      <td key={t} className="p-2 text-xs">
                        <div className="flex items-center gap-1">
                          <span
                            className={`inline-block w-2 h-2 rounded ${COLOR_MAP[t]}`}
                          />
                          {v}
                        </div>
                      </td>
                    );
                  })}
                </tr>
              ))}
              {data.buckets.length === 0 && (
                <tr>
                  <td colSpan={6} className="p-4 text-center text-gray-500">
                    범위 내 데이터 없음
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        )}
      </section>

      <section>
        <h2 className="font-bold mb-2">단건 segments 조회</h2>
        <div className="flex gap-2 mb-3">
          <input
            value={messageId}
            onChange={(e) => setMessageId(e.target.value)}
            placeholder="message_id (= request_id)"
            className="flex-1 px-2 py-1 border rounded text-sm"
          />
          <button
            onClick={handleLoadSegments}
            className="px-3 py-1 border rounded bg-blue-50 text-sm"
          >
            조회
          </button>
        </div>
        {segments ? (
          <pre className="bg-gray-50 border rounded p-3 text-xs overflow-x-auto max-h-96">
            {JSON.stringify(segments, null, 2)}
          </pre>
        ) : null}
      </section>
    </div>
  );
}
