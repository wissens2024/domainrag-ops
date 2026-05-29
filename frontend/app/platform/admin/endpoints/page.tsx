/**
 * Platform Endpoints — /platform/admin/endpoints (ADR-017 §18).
 */
'use client';

import useSWR from 'swr';
import Card from '@/components/ui/Card';
import { listPlatformEndpoints } from '@/lib/api';

export default function PlatformEndpointsPage() {
  const { data, isLoading, error } = useSWR(
    'platform-endpoints',
    () => listPlatformEndpoints(),
    { refreshInterval: 30000 },
  );

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4 text-gray-900 dark:text-slate-100">Endpoints</h1>
      {isLoading && <p className="text-gray-500 dark:text-slate-400">로딩...</p>}
      {error && <p className="text-red-600 dark:text-red-400">로드 실패: {String(error)}</p>}
      {data && (
        <Card padded={false} className="overflow-hidden">
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b border-gray-200 dark:border-slate-700 bg-gray-50 dark:bg-slate-900/50 text-left text-gray-500 dark:text-slate-400">
              <th className="p-2">name</th>
              <th className="p-2">URL</th>
              <th className="p-2">kind</th>
              <th className="p-2">status</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((e) => (
              <tr key={e.name} className="border-b border-gray-100 dark:border-slate-700/60">
                <td className="p-2 font-mono text-xs">{e.name}</td>
                <td className="p-2 font-mono text-xs">{e.url}</td>
                <td className="p-2 text-xs">{e.kind}</td>
                <td className="p-2 text-xs">{e.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
        </Card>
      )}
    </div>
  );
}
