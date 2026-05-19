/**
 * Tenant Configs — /{tid}/admin/configs (ADR-016 §3.8 + ADR-017 §11).
 *
 * 카테고리별 dotted path key + value 편집 + history. PLATFORM_ADMIN_RESTRICTED 키 안내.
 */
'use client';

import { useParams } from 'next/navigation';
import { useState } from 'react';
import useSWR, { mutate } from 'swr';
import {
  getConfigCategory,
  getConfigHistory,
  patchConfig,
  reloadConfig,
} from '@/lib/api';

const CATEGORIES = [
  'citation',
  'retrieval',
  'model',
  'routing',
  'query_classifier',
  'lifecycle',
  'auth',
  'pii',
  'audit',
  'data_retention',
];

export default function TenantConfigsPage() {
  const params = useParams<{ tenantId: string }>();
  const tenantId = params.tenantId;
  const [category, setCategory] = useState('citation');
  const [pathInput, setPathInput] = useState('');
  const [valueInput, setValueInput] = useState('');
  const [reason, setReason] = useState('');
  const [message, setMessage] = useState<{ type: 'ok' | 'error'; text: string } | null>(null);

  const swrKey = `configs:${tenantId}:${category}`;
  const { data, isLoading } = useSWR<Record<string, unknown>>(
    tenantId ? swrKey : null,
    () => getConfigCategory(tenantId, category),
  );

  const historyKey = `configs-history:${tenantId}:${category}`;
  const { data: history } = useSWR(
    tenantId ? historyKey : null,
    () => getConfigHistory(tenantId, { category, page_size: 10 }),
  );

  const handlePatch = async () => {
    setMessage(null);
    let parsed: unknown;
    try {
      parsed = JSON.parse(valueInput);
    } catch {
      parsed = valueInput;
    }
    try {
      await patchConfig(tenantId, category, {
        key: pathInput,
        value: parsed,
        reason,
      });
      setMessage({ type: 'ok', text: '저장 완료. 즉시 반영.' });
      setPathInput('');
      setValueInput('');
      setReason('');
      void mutate(swrKey);
      void mutate(historyKey);
    } catch (e) {
      if (e instanceof Error && e.message === 'config_key_restricted') {
        setMessage({
          type: 'error',
          text: '이 key는 PLATFORM_ADMIN 전용입니다. /platform/admin/configs에서 변경하세요.',
        });
      } else {
        setMessage({ type: 'error', text: e instanceof Error ? e.message : 'failed' });
      }
    }
  };

  const handleReload = async () => {
    try {
      await reloadConfig(tenantId);
      void mutate(swrKey);
      setMessage({ type: 'ok', text: 'cache 무효화 완료' });
    } catch (e) {
      setMessage({ type: 'error', text: e instanceof Error ? e.message : '' });
    }
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">Tenant Configs</h1>

      <div className="flex gap-2 mb-4 items-center text-sm">
        <select
          value={category}
          onChange={(e) => setCategory(e.target.value)}
          className="px-3 py-1 border rounded"
        >
          {CATEGORIES.map((c) => (
            <option key={c} value={c}>
              {c}
            </option>
          ))}
        </select>
        <button
          onClick={handleReload}
          className="px-3 py-1 border rounded bg-gray-50"
        >
          ⟳ reload
        </button>
      </div>

      <div className="grid grid-cols-2 gap-6">
        <section>
          <h2 className="font-bold mb-2">현재 effective {category}</h2>
          {isLoading && <p>로딩...</p>}
          <pre className="bg-gray-50 border rounded p-3 text-xs overflow-x-auto max-h-96">
            {data ? JSON.stringify(data, null, 2) : '-'}
          </pre>
        </section>

        <section>
          <h2 className="font-bold mb-2">패치 (dotted path)</h2>
          <div className="space-y-2 text-sm">
            <input
              value={pathInput}
              onChange={(e) => setPathInput(e.target.value)}
              placeholder="예: verification.tier2.thresholds.strong"
              className="w-full px-2 py-1 border rounded font-mono text-xs"
            />
            <textarea
              value={valueInput}
              onChange={(e) => setValueInput(e.target.value)}
              placeholder="JSON 또는 raw (예: 0.78)"
              rows={4}
              className="w-full px-2 py-1 border rounded font-mono text-xs"
            />
            <input
              value={reason}
              onChange={(e) => setReason(e.target.value)}
              placeholder="reason (audit 기록용)"
              className="w-full px-2 py-1 border rounded text-xs"
            />
            <button
              onClick={handlePatch}
              className="px-3 py-1 bg-blue-600 text-white rounded"
            >
              저장
            </button>
            {message && (
              <p
                className={`text-sm ${
                  message.type === 'ok' ? 'text-green-700' : 'text-red-600'
                }`}
              >
                {message.text}
              </p>
            )}
          </div>

          <h3 className="font-bold mt-6 mb-2">최근 변경 (history)</h3>
          <ul className="text-xs space-y-1 max-h-80 overflow-y-auto">
            {history?.items?.map((h, i) => (
              <li key={i} className="border-b pb-1">
                <span className="font-mono">{h.path}</span>
                <span className="text-gray-500">
                  {' '}
                  · {h.author} · {new Date(h.changed_at).toLocaleString('ko-KR')}
                </span>
                <details className="ml-2">
                  <summary className="cursor-pointer">diff</summary>
                  <div className="text-xs bg-gray-50 p-1">
                    old: {JSON.stringify(h.old_value)}
                  </div>
                  <div className="text-xs bg-blue-50 p-1">
                    new: {JSON.stringify(h.new_value)}
                  </div>
                  {h.reason && <div className="text-gray-500">reason: {h.reason}</div>}
                </details>
              </li>
            ))}
          </ul>
        </section>
      </div>
    </div>
  );
}
