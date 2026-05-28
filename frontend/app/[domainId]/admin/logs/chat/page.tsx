/**
 * Chat Logs — /{tid}/admin/logs/chat (ADR-016 §3.5 + ADR-017 §8).
 *
 * 목록 + 다중 필터 + 단건 상세 (retrieved_chunks / verifier_metrics / routing_decision).
 */
'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useState } from 'react';
import useSWR from 'swr';
import { listChatLogs } from '@/lib/api';
import { useLanguage } from '@/components/LanguageProvider';
import type { ChatLogListResult, ChatLogRow, SupportType, UiMode } from '@/lib/types';

const SUPPORT_TYPE_COLORS: Record<SupportType, string> = {
  direct: 'bg-citation-direct text-white',
  synthesis: 'bg-citation-synthesis text-white',
  inference: 'bg-citation-inference text-white',
  conflict: 'bg-citation-conflict text-white',
};

export default function ChatLogsPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const { t } = useLanguage();
  const [filters, setFilters] = useState({
    keyword: '',
    user_id: '',
    citation_type: '' as SupportType | '',
    ui_mode: '' as UiMode | '',
    fallback_only: false,
    min_confidence: '',
    max_confidence: '',
  });
  const [page, setPage] = useState(1);
  const [selectedRow, setSelectedRow] = useState<ChatLogRow | null>(null);
  const pageSize = 30;

  const swrKey = `chat_logs:${domainId}:${JSON.stringify(filters)}:${page}`;
  const { data, isLoading, error } = useSWR<ChatLogListResult>(
    domainId ? swrKey : null,
    () =>
      listChatLogs(domainId, {
        keyword: filters.keyword || undefined,
        user_id: filters.user_id || undefined,
        citation_type: filters.citation_type || undefined,
        ui_mode: filters.ui_mode || undefined,
        fallback_only: filters.fallback_only || undefined,
        min_confidence: filters.min_confidence ? Number(filters.min_confidence) : undefined,
        max_confidence: filters.max_confidence ? Number(filters.max_confidence) : undefined,
        page,
        page_size: pageSize,
      }),
  );

  const update = <K extends keyof typeof filters>(k: K, v: (typeof filters)[K]) => {
    setFilters((f) => ({ ...f, [k]: v }));
    setPage(1);
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">{t('chatLogs.title')}</h1>

      <div className="grid grid-cols-6 gap-2 mb-4 text-sm">
        <input
          placeholder={t('chatLogs.keyword')}
          value={filters.keyword}
          onChange={(e) => update('keyword', e.target.value)}
          className="px-2 py-1 border rounded col-span-2"
        />
        <input
          placeholder={t('chatLogs.userId')}
          value={filters.user_id}
          onChange={(e) => update('user_id', e.target.value)}
          className="px-2 py-1 border rounded"
        />
        <select
          value={filters.citation_type}
          onChange={(e) => update('citation_type', e.target.value as SupportType | '')}
          className="px-2 py-1 border rounded"
        >
          <option value="">{t('chatLogs.allTypes')}</option>
          <option value="direct">direct</option>
          <option value="synthesis">synthesis</option>
          <option value="inference">inference</option>
          <option value="conflict">conflict</option>
        </select>
        <select
          value={filters.ui_mode}
          onChange={(e) => update('ui_mode', e.target.value as UiMode | '')}
          className="px-2 py-1 border rounded"
        >
          <option value="">{t('chatLogs.allModes')}</option>
          <option value="chat_structured">structured</option>
          <option value="chat_streaming">streaming</option>
        </select>
        <label className="px-2 py-1 text-xs">
          <input
            type="checkbox"
            checked={filters.fallback_only}
            onChange={(e) => update('fallback_only', e.target.checked)}
            className="mr-1"
          />
          {t('chatLogs.fallbackOnly')}
        </label>
        <input
          placeholder={t('chatLogs.minConf')}
          type="number"
          step="0.05"
          value={filters.min_confidence}
          onChange={(e) => update('min_confidence', e.target.value)}
          className="px-2 py-1 border rounded"
        />
        <input
          placeholder={t('chatLogs.maxConf')}
          type="number"
          step="0.05"
          value={filters.max_confidence}
          onChange={(e) => update('max_confidence', e.target.value)}
          className="px-2 py-1 border rounded"
        />
        <Link
          href={`/${domainId}/admin/citation-inspector`}
          className="text-xs text-blue-600 hover:underline self-center col-span-2"
        >
          {t('chatLogs.toInspector')}
        </Link>
      </div>

      {isLoading && <p>{t('common.loading')}</p>}
      {error && <p className="text-red-600">{t('common.loadFailed')}: {error.message}</p>}

      {data && (
        <div className="flex gap-4">
          <div className="flex-1">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="border-b bg-gray-50 text-left">
                  <th className="p-2">{t('chatLogs.colTime')}</th>
                  <th className="p-2">{t('chatLogs.colUser')}</th>
                  <th className="p-2">{t('chatLogs.colQuestion')}</th>
                  <th className="p-2">{t('chatLogs.colConfidence')}</th>
                  <th className="p-2">{t('chatLogs.colTypes')}</th>
                  <th className="p-2">{t('chatLogs.colFeedback')}</th>
                  <th className="p-2">{t('chatLogs.colMode')}</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((r) => (
                  <tr
                    key={r.request_id}
                    className={`border-b hover:bg-gray-50 cursor-pointer ${
                      selectedRow?.request_id === r.request_id ? 'bg-blue-50' : ''
                    }`}
                    onClick={() => setSelectedRow(r)}
                  >
                    <td className="p-2 text-xs">
                      {new Date(r.created_at).toLocaleString('ko-KR')}
                    </td>
                    <td className="p-2 text-xs">{r.user_id?.slice(0, 12) || '-'}</td>
                    <td className="p-2 text-xs truncate max-w-xs">
                      {r.fallback_reason && (
                        <span className="text-yellow-600 text-xs">⚠ </span>
                      )}
                      {r.question?.slice(0, 80) || '(deleted)'}
                    </td>
                    <td className="p-2 text-xs">
                      {r.confidence !== null ? r.confidence.toFixed(2) : '-'}
                    </td>
                    <td className="p-2 text-xs space-x-0.5">
                      {(r.citation_types || []).map((t) => (
                        <span
                          key={t}
                          className={`px-1 rounded text-xs ${SUPPORT_TYPE_COLORS[t]}`}
                          title={t}
                        >
                          {t[0]}
                        </span>
                      ))}
                    </td>
                    <td className="p-2 text-xs">
                      {r.feedback === 'good' && '👍'}
                      {r.feedback === 'bad' && '👎'}
                      {!r.feedback && '-'}
                    </td>
                    <td className="p-2 text-xs">
                      {r.ui_mode === 'chat_structured'
                        ? t('chatLogs.modeStructured')
                        : t('chatLogs.modeStreaming')}
                    </td>
                  </tr>
                ))}
                {data.items.length === 0 && (
                  <tr>
                    <td colSpan={7} className="p-4 text-center text-gray-500">
                      {t('chatLogs.empty')}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>

            <div className="mt-4 flex justify-between items-center text-sm">
              <span>
                {t('common.total')} {data.total}{t('common.count')} · {page} /{' '}
                {Math.max(1, Math.ceil(data.total / pageSize))}
              </span>
              <div className="flex gap-2">
                <button
                  disabled={page <= 1}
                  onClick={() => setPage(page - 1)}
                  className="px-2 py-1 border rounded disabled:bg-gray-100"
                >
                  {t('common.prev')}
                </button>
                <button
                  disabled={page * pageSize >= data.total}
                  onClick={() => setPage(page + 1)}
                  className="px-2 py-1 border rounded disabled:bg-gray-100"
                >
                  {t('common.next')}
                </button>
              </div>
            </div>
          </div>

          {selectedRow && (
            <aside className="w-96 border rounded p-3 text-xs h-fit sticky top-4 overflow-y-auto max-h-screen">
              <div className="flex justify-between mb-2">
                <span className="font-bold">{t('common.detail')}</span>
                <button
                  onClick={() => setSelectedRow(null)}
                  className="text-gray-500"
                >
                  ✕
                </button>
              </div>
              <DetailField label="request_id" value={selectedRow.request_id} mono />
              <DetailField
                label="conversation_id"
                value={selectedRow.conversation_id || '-'}
                mono
              />
              <DetailField label="user_id" value={selectedRow.user_id || '-'} />
              <DetailField label="question" value={selectedRow.question || '-'} />
              <DetailField
                label="rewritten_query"
                value={selectedRow.rewritten_query || '-'}
              />
              <DetailField label="answer" value={selectedRow.answer || '-'} />
              <DetailField
                label="confidence"
                value={selectedRow.confidence?.toFixed(4) || '-'}
              />
              <DetailField
                label="fallback_reason"
                value={selectedRow.fallback_reason || '-'}
              />
              <DetailField label="latency_ms" value={String(selectedRow.latency_ms || '-')} />
              <DetailField
                label="model_failure_chain"
                value={(selectedRow.model_failure_chain || []).join(', ') || '-'}
              />
              <DetailField
                label="input_pii_found"
                value={(selectedRow.input_pii_found || []).join(', ') || '-'}
              />
              <DetailField
                label="output_pii_masked"
                value={(selectedRow.output_pii_masked || []).join(', ') || '-'}
              />
              <details className="mt-2">
                <summary className="font-bold cursor-pointer">
                  routing_decision
                </summary>
                <pre className="text-xs">
                  {JSON.stringify(selectedRow.routing_decision, null, 2)}
                </pre>
              </details>
              <details className="mt-2">
                <summary className="font-bold cursor-pointer">
                  verifier_metrics
                </summary>
                <pre className="text-xs">
                  {JSON.stringify(selectedRow.verifier_metrics, null, 2)}
                </pre>
              </details>
              <details className="mt-2">
                <summary className="font-bold cursor-pointer">
                  retrieved_chunks
                </summary>
                <pre className="text-xs">
                  {JSON.stringify(selectedRow.retrieved_chunks, null, 2)}
                </pre>
              </details>
              <details className="mt-2">
                <summary className="font-bold cursor-pointer">citations</summary>
                <pre className="text-xs">
                  {JSON.stringify(selectedRow.citations, null, 2)}
                </pre>
              </details>
            </aside>
          )}
        </div>
      )}
    </div>
  );
}

function DetailField({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div className="mb-1">
      <span className="text-gray-500">{label}: </span>
      <span className={mono ? 'font-mono' : ''}>{value}</span>
    </div>
  );
}
