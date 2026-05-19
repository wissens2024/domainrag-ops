/**
 * Platform Endpoints — /platform/admin/endpoints (ADR-017 §18).
 */
'use client';

import useSWR from 'swr';
import { listPlatformEndpoints } from '@/lib/api';

export default function PlatformEndpointsPage() {
  const { data, isLoading, error } = useSWR(
    'platform-endpoints',
    () => listPlatformEndpoints(),
    { refreshInterval: 30000 },
  );

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">Endpoints</h1>
      {isLoading && <p>로딩...</p>}
      {error && <p className="text-red-600">로드 실패: {String(error)}</p>}
      {data && (
        <table className="w-full text-sm">
          <thead>
            <tr className="border-b bg-gray-50 text-left">
              <th className="p-2">name</th>
              <th className="p-2">URL</th>
              <th className="p-2">backend</th>
              <th className="p-2">status</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((e) => (
              <tr key={e.name} className="border-b">
                <td className="p-2 font-mono text-xs">{e.name}</td>
                <td className="p-2 font-mono text-xs">{e.url}</td>
                <td className="p-2 text-xs">{e.backend}</td>
                <td className="p-2 text-xs">{e.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
