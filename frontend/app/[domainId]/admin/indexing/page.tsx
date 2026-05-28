/**
 * Indexing Jobs — /{tid}/admin/indexing (ADR-016 §3.3 + ADR-017 §7).
 *
 * 잡 목록 + 상태 필터 + 진행률 + failed chunks 표시 + retry.
 * 디자인 시스템(ui/) + i18n 적용 (ADR-016 보강).
 */
'use client';

import { Fragment, useState } from 'react';
import { useParams } from 'next/navigation';
import useSWR, { mutate } from 'swr';
import Card from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import Badge from '@/components/ui/Badge';
import { useLanguage } from '@/components/LanguageProvider';
import { listIndexingJobs, retryIndexingJob } from '@/lib/api';
import type { IndexingJob, IndexingJobListResult, IndexingStatus } from '@/lib/types';

type Tone = 'neutral' | 'info' | 'success' | 'warn' | 'danger';
const STATUS_TONE: Record<IndexingStatus, Tone> = {
  pending: 'neutral',
  parsing: 'info',
  chunking: 'info',
  embedding: 'info',
  indexing: 'info',
  completed: 'success',
  partial: 'warn',
  failed: 'danger',
};

const STATUS_OPTIONS: IndexingStatus[] = [
  'pending', 'parsing', 'chunking', 'embedding', 'indexing', 'completed', 'partial', 'failed',
];

export default function IndexingPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const { t } = useLanguage();
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

  const total = data?.total ?? 0;

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <h1 className="text-2xl font-bold mb-4 text-gray-900 dark:text-slate-100">
        {t('indexing.title')}
      </h1>

      <div className="flex gap-2 mb-4">
        <select
          value={statusFilter}
          onChange={(e) => {
            setStatusFilter(e.target.value as IndexingStatus | '');
            setPage(1);
          }}
          className="px-3 py-2 border border-gray-300 rounded-lg text-sm bg-white dark:bg-slate-900 dark:border-slate-600 dark:text-slate-100"
        >
          <option value="">{t('indexing.allStatus')}</option>
          {STATUS_OPTIONS.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
      </div>

      {isLoading && <p className="text-gray-500 dark:text-slate-400">{t('common.loading')}</p>}
      {error && (
        <p className="text-red-600 dark:text-red-400">
          {t('common.loadFailed')}: {error.message}
        </p>
      )}

      {data && (
        <>
          <Card padded={false} className="overflow-hidden">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="border-b border-gray-200 dark:border-slate-700 bg-gray-50 dark:bg-slate-900/50 text-left text-gray-500 dark:text-slate-400">
                  <th className="p-3 font-medium">job_id / doc_id</th>
                  <th className="p-3 font-medium">mode</th>
                  <th className="p-3 font-medium">{t('indexing.colStatus')}</th>
                  <th className="p-3 font-medium">{t('indexing.colProgress')}</th>
                  <th className="p-3 font-medium">indexed / failed</th>
                  <th className="p-3 font-medium">{t('indexing.colStarted')}</th>
                  <th className="p-3 font-medium">{t('indexing.colActions')}</th>
                </tr>
              </thead>
              <tbody>
                {data.items.map((j) => (
                  <Fragment key={j.job_id}>
                    <tr className="border-b border-gray-100 dark:border-slate-700/60 hover:bg-gray-50 dark:hover:bg-slate-700/40">
                      <td className="p-3">
                        <div className="font-mono text-xs text-gray-900 dark:text-slate-200">
                          {j.job_id.slice(0, 12)}…
                        </div>
                        <div className="text-xs text-gray-500 dark:text-slate-400">
                          {j.doc_id} v{j.doc_version}
                        </div>
                      </td>
                      <td className="p-3 text-xs">{j.mode}</td>
                      <td className="p-3">
                        <Badge tone={STATUS_TONE[j.status]}>{j.status}</Badge>
                      </td>
                      <td className="p-3">
                        <div className="h-2 bg-gray-100 dark:bg-slate-700 rounded w-24 overflow-hidden">
                          <div
                            className="h-full bg-blue-500 dark:bg-brand-500"
                            style={{ width: `${Math.round(j.progress * 100)}%` }}
                          />
                        </div>
                        <div className="text-xs text-gray-500 dark:text-slate-400">
                          {Math.round(j.progress * 100)}%
                        </div>
                      </td>
                      <td className="p-3 text-xs">
                        {j.indexed_chunks} / {j.failed_chunks.length}
                      </td>
                      <td className="p-3 text-xs">
                        {j.started_at ? new Date(j.started_at).toLocaleString('ko-KR') : '-'}
                      </td>
                      <td className="p-3 text-xs space-x-1 whitespace-nowrap">
                        <Button
                          variant="secondary"
                          size="sm"
                          onClick={() => setExpanded(expanded === j.job_id ? null : j.job_id)}
                        >
                          {expanded === j.job_id ? t('indexing.collapse') : t('indexing.expand')}
                        </Button>
                        {(j.status === 'failed' || j.status === 'partial') && (
                          <Button variant="secondary" size="sm" onClick={() => void handleRetry(j)}>
                            retry
                          </Button>
                        )}
                      </td>
                    </tr>
                    {expanded === j.job_id && (
                      <tr className="bg-gray-50 dark:bg-slate-900/40">
                        <td colSpan={7} className="p-3 text-xs text-gray-700 dark:text-slate-300">
                          {j.error_message && (
                            <div className="text-red-600 dark:text-red-400 mb-2">
                              error: {j.error_message}
                            </div>
                          )}
                          {j.failure_rate !== null && (
                            <div>failure_rate: {j.failure_rate.toFixed(2)}</div>
                          )}
                          {j.failed_chunks.length > 0 && (
                            <details className="mt-2">
                              <summary>failed_chunks ({j.failed_chunks.length})</summary>
                              <pre className="text-xs mt-1 max-h-40 overflow-y-auto">
                                {JSON.stringify(j.failed_chunks, null, 2)}
                              </pre>
                            </details>
                          )}
                        </td>
                      </tr>
                    )}
                  </Fragment>
                ))}
                {data.items.length === 0 && (
                  <tr>
                    <td colSpan={7} className="p-6 text-center text-gray-500 dark:text-slate-400">
                      {t('indexing.empty')}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
          </Card>

          <div className="mt-4 flex justify-between items-center text-sm text-gray-600 dark:text-slate-300">
            <span>
              {t('common.total')} {total}
              {t('common.count')} · {page} / {Math.max(1, Math.ceil(total / pageSize))}
            </span>
            <div className="flex gap-2">
              <Button variant="secondary" size="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                {t('common.prev')}
              </Button>
              <Button
                variant="secondary"
                size="sm"
                disabled={page * pageSize >= total}
                onClick={() => setPage(page + 1)}
              >
                {t('common.next')}
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
