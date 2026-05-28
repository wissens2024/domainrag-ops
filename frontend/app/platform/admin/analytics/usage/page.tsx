/**
 * Platform Usage Analytics — /platform/admin/analytics/usage (ADR-017 §18).
 */
'use client';

import useSWR from 'swr';
import Card from '@/components/ui/Card';
import { getPlatformUsage } from '@/lib/api';

export default function UsageAnalyticsPage() {
  const { data, isLoading } = useSWR('platform-usage', getPlatformUsage);

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4 text-gray-900 dark:text-slate-100">Usage Analytics</h1>
      {isLoading && <p className="text-gray-500 dark:text-slate-400">로딩...</p>}
      {data && (
        <Card padded={false} className="overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 dark:border-slate-700 bg-gray-50 dark:bg-slate-900/50 text-left text-gray-500 dark:text-slate-400">
              <th className="p-2">domain_id</th>
              <th className="p-2">messages</th>
              <th className="p-2">fallbacks</th>
              <th className="p-2">avg_latency_ms</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((u) => (
              <tr key={u.domain_id} className="border-b border-gray-100 dark:border-slate-700/60">
                <td className="p-2 font-mono text-xs">{u.domain_id}</td>
                <td className="p-2 text-xs">{u.messages.toLocaleString()}</td>
                <td className="p-2 text-xs">{u.fallbacks.toLocaleString()}</td>
                <td className="p-2 text-xs">{u.avg_latency_ms.toFixed(0)}</td>
              </tr>
            ))}
            {data.items.length === 0 && (
              <tr>
                <td colSpan={4} className="p-4 text-center text-gray-500 dark:text-slate-400">
                  사용 통계 없음
                </td>
              </tr>
            )}
          </tbody>
        </table>
        </Card>
      )}
    </div>
  );
}
