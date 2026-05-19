/**
 * Health Metrics — /platform/admin/health (ADR-021 후속 결선).
 *
 * ledger + chat_log writer 실패 누적 + 최근 dead-letter.
 */
'use client';

import useSWR from 'swr';
import { getHealthMetrics } from '@/lib/api';

export default function PlatformHealthMetricsPage() {
  const { data, isLoading, error } = useSWR(
    'platform-health-metrics',
    () => getHealthMetrics(),
    { refreshInterval: 30000 },
  );

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">Health Metrics (이 process 한정)</h1>

      {isLoading && <p>로딩...</p>}
      {error && <p className="text-red-600">로드 실패: {String(error)}</p>}

      {data && (
        <div className="grid grid-cols-2 gap-6">
          <section className="border rounded p-4">
            <h2 className="font-bold mb-2">Ledger publish</h2>
            <p className="text-3xl font-bold">{data.ledger.publish_failures_total}</p>
            <p className="text-sm text-gray-500">총 실패 건</p>
            <p className="text-xs text-gray-500 mt-2">
              dead-letter: {data.ledger.dead_letter_count}건 (최근 10건 노출)
            </p>
            <pre className="text-xs bg-gray-50 p-2 mt-2 max-h-60 overflow-y-auto">
              {JSON.stringify(data.ledger.recent_dead_letters, null, 2)}
            </pre>
          </section>

          <section className="border rounded p-4">
            <h2 className="font-bold mb-2">chat_log writer</h2>
            <p className="text-3xl font-bold">
              {data.chat_log_writer.write_failures_total}
            </p>
            <p className="text-sm text-gray-500">총 실패 건</p>
            <p className="text-xs text-gray-500 mt-2">
              dead-letter: {data.chat_log_writer.dead_letter_count}건
            </p>
            <pre className="text-xs bg-gray-50 p-2 mt-2 max-h-60 overflow-y-auto">
              {JSON.stringify(data.chat_log_writer.recent_dead_letters, null, 2)}
            </pre>
          </section>
        </div>
      )}
    </div>
  );
}
