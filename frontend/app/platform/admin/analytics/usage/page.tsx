/**
 * Platform Usage Analytics — /platform/admin/analytics/usage (ADR-017 §18).
 */
'use client';

import useSWR from 'swr';
import { getPlatformUsage } from '@/lib/api';

export default function UsageAnalyticsPage() {
  const { data, isLoading } = useSWR('platform-usage', getPlatformUsage);

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">Usage Analytics</h1>
      {isLoading && <p>로딩...</p>}
      {data && (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-gray-50 text-left">
              <th className="p-2">tenant_id</th>
              <th className="p-2">messages</th>
              <th className="p-2">fallbacks</th>
              <th className="p-2">avg_latency_ms</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((u) => (
              <tr key={u.tenant_id} className="border-b">
                <td className="p-2 font-mono text-xs">{u.tenant_id}</td>
                <td className="p-2 text-xs">{u.messages.toLocaleString()}</td>
                <td className="p-2 text-xs">{u.fallbacks.toLocaleString()}</td>
                <td className="p-2 text-xs">{u.avg_latency_ms.toFixed(0)}</td>
              </tr>
            ))}
            {data.items.length === 0 && (
              <tr>
                <td colSpan={4} className="p-4 text-center text-gray-500">
                  사용 통계 없음
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
