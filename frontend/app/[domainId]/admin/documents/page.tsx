/**
 * Document Management — /{tid}/admin/documents (ADR-016 §3.2 + ADR-017 §6).
 *
 * 목록 + 검색·필터·페이징 + reindex(4 mode) + approval patch + upload 진입점.
 * 디자인 시스템(ui/) + i18n 적용 (ADR-016 보강).
 */
'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useState } from 'react';
import useSWR, { mutate } from 'swr';
import Card from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import Badge from '@/components/ui/Badge';
import { useLanguage } from '@/components/LanguageProvider';
import { listDocuments, patchDocumentApproval, reindexDocument } from '@/lib/api';
import type { ApprovalStatus, DocumentListResult, ReindexMode } from '@/lib/types';

const STATUS_TONE: Record<ApprovalStatus, 'warn' | 'success' | 'neutral'> = {
  pending: 'warn',
  approved: 'success',
  archived: 'neutral',
};

const REINDEX_OPTIONS: { mode: ReindexMode; label: string; desc: string }[] = [
  { mode: 'full', label: 'FULL', desc: '파싱부터 임베딩까지 전체 재처리' },
  { mode: 'chunk_re_split', label: 'CHUNK_RE_SPLIT', desc: 'chunking 부터 재처리' },
  { mode: 'embedding_only', label: 'EMBEDDING_ONLY', desc: 'chunks 보존 + vectors 재계산' },
  { mode: 'parser_only', label: 'PARSER_ONLY', desc: 'metadata만 갱신' },
];

export default function DocumentsPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const { t } = useLanguage();
  const [keyword, setKeyword] = useState('');
  const [approval, setApproval] = useState<ApprovalStatus | ''>('');
  const [page, setPage] = useState(1);
  const pageSize = 20;
  const [reindexFor, setReindexFor] = useState<string | null>(null);

  const statusLabel: Record<ApprovalStatus, string> = {
    pending: t('documents.statusPending'),
    approved: t('documents.statusApproved'),
    archived: t('documents.statusArchived'),
  };

  const swrKey = `documents:${domainId}:${page}:${pageSize}:${keyword}:${approval}`;
  const { data, isLoading, error } = useSWR<DocumentListResult>(
    domainId ? swrKey : null,
    () =>
      listDocuments(domainId, {
        keyword: keyword || undefined,
        approval_status: approval || undefined,
        page,
        page_size: pageSize,
      }),
  );

  const handleReindex = async (docId: string, mode: ReindexMode) => {
    try {
      await reindexDocument(domainId, docId, mode);
      alert(`reindex 요청됨 (mode=${mode}). 진행 상황은 인덱싱 모니터링에서.`);
      setReindexFor(null);
    } catch (e) {
      alert(`reindex 실패: ${e instanceof Error ? e.message : '알 수 없는 오류'}`);
    }
  };

  const handleApprove = async (docId: string, status: ApprovalStatus) => {
    if (!confirm(`approval_status='${status}'으로 변경?`)) return;
    try {
      await patchDocumentApproval(domainId, docId, { status });
      void mutate(swrKey);
    } catch (e) {
      alert(`approval 변경 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6 max-w-7xl mx-auto">
      <div className="flex justify-between items-center mb-4">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-slate-100">
          {t('documents.title')}
        </h1>
        <Link
          href={`/${domainId}/admin/documents/upload`}
          className="inline-flex items-center gap-1.5 px-4 py-2 text-sm font-medium rounded-lg bg-gray-900 text-white hover:bg-gray-800 dark:bg-brand-600 dark:hover:bg-brand-500"
        >
          {t('documents.upload')}
        </Link>
      </div>

      <div className="flex gap-2 mb-4">
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && setPage(1)}
          placeholder={t('documents.searchPlaceholder')}
          className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm bg-white dark:bg-slate-900 dark:border-slate-600 dark:text-slate-100"
        />
        <select
          value={approval}
          onChange={(e) => {
            setApproval(e.target.value as ApprovalStatus | '');
            setPage(1);
          }}
          className="px-3 py-2 border border-gray-300 rounded-lg text-sm bg-white dark:bg-slate-900 dark:border-slate-600 dark:text-slate-100"
        >
          <option value="">{t('documents.allApproval')}</option>
          <option value="pending">{t('documents.statusPending')}</option>
          <option value="approved">{t('documents.statusApproved')}</option>
          <option value="archived">{t('documents.statusArchived')}</option>
        </select>
      </div>

      {isLoading && <p className="text-gray-500 dark:text-slate-400">{t('common.loading')}</p>}
      {error && (
        <p className="text-red-600 dark:text-red-400">
          {t('common.loadFailed')}: {error.message}
        </p>
      )}

      {data && (
        <Card padded={false} className="overflow-hidden">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b border-gray-200 dark:border-slate-700 bg-gray-50 dark:bg-slate-900/50 text-left text-gray-500 dark:text-slate-400">
                <th className="p-3 font-medium">{t('documents.colName')}</th>
                <th className="p-3 font-medium">{t('documents.colInputType')}</th>
                <th className="p-3 font-medium">{t('documents.colDept')}</th>
                <th className="p-3 font-medium">{t('documents.colSecurity')}</th>
                <th className="p-3 font-medium">{t('documents.colVersion')}</th>
                <th className="p-3 font-medium">{t('documents.colChunks')}</th>
                <th className="p-3 font-medium">{t('documents.colStatus')}</th>
                <th className="p-3 font-medium">{t('documents.colIndexed')}</th>
                <th className="p-3 font-medium">{t('documents.colActions')}</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((d) => (
                <tr
                  key={d.doc_id}
                  className="border-b border-gray-100 dark:border-slate-700/60 hover:bg-gray-50 dark:hover:bg-slate-700/40"
                >
                  <td className="p-3">
                    <Link
                      href={`/${domainId}/admin/documents/${d.doc_id}`}
                      className="font-medium text-blue-600 dark:text-brand-400 hover:underline"
                    >
                      {d.title}
                    </Link>
                    <div className="text-xs text-gray-500 dark:text-slate-400">{d.doc_id}</div>
                  </td>
                  <td className="p-3 text-xs">{d.input_type || '-'}</td>
                  <td className="p-3 text-xs">{d.department || '-'}</td>
                  <td className="p-3 text-xs">{d.security_level || '-'}</td>
                  <td className="p-3 text-xs">{d.version}</td>
                  <td className="p-3 text-xs">{d.chunk_count}</td>
                  <td className="p-3">
                    <Badge tone={STATUS_TONE[d.approval_status]}>
                      {statusLabel[d.approval_status]}
                    </Badge>
                  </td>
                  <td className="p-3 text-xs">
                    {d.last_indexed_at
                      ? new Date(d.last_indexed_at).toLocaleDateString('ko-KR')
                      : '-'}
                  </td>
                  <td className="p-3 text-xs space-x-1 whitespace-nowrap">
                    <Button
                      variant="secondary"
                      size="sm"
                      onClick={() =>
                        setReindexFor(reindexFor === d.doc_id ? null : d.doc_id)
                      }
                    >
                      {t('documents.reindex')}
                    </Button>
                    {d.approval_status !== 'approved' && (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => handleApprove(d.doc_id, 'approved')}
                      >
                        {t('documents.approve')}
                      </Button>
                    )}
                    {d.approval_status !== 'archived' && (
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => handleApprove(d.doc_id, 'archived')}
                      >
                        {t('documents.archive')}
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
              {data.items.length === 0 && (
                <tr>
                  <td colSpan={9} className="p-6 text-center text-gray-500 dark:text-slate-400">
                    {t('documents.empty')}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </Card>
      )}

      {reindexFor && (
        <Card className="mt-4">
          <p className="text-sm font-bold mb-2 text-gray-900 dark:text-slate-100">
            {t('documents.reindexSelect')} ({reindexFor})
          </p>
          <div className="grid grid-cols-2 gap-2">
            {REINDEX_OPTIONS.map((opt) => (
              <button
                key={opt.mode}
                onClick={() => void handleReindex(reindexFor, opt.mode)}
                className="text-left p-2 border border-gray-200 dark:border-slate-600 rounded-lg hover:bg-gray-50 dark:hover:bg-slate-700"
              >
                <span className="font-mono text-xs font-bold text-gray-900 dark:text-slate-100">
                  {opt.label}
                </span>
                <p className="text-xs text-gray-500 dark:text-slate-400">{opt.desc}</p>
              </button>
            ))}
          </div>
          <Button
            variant="ghost"
            size="sm"
            className="mt-2"
            onClick={() => setReindexFor(null)}
          >
            {t('documents.cancel')}
          </Button>
        </Card>
      )}

      {data && (
        <div className="mt-4 flex justify-between items-center text-sm text-gray-600 dark:text-slate-300">
          <span>
            {t('common.total')} {data.total ?? 0}
            {t('common.count')} · {page} /{' '}
            {Math.max(1, Math.ceil((data.total ?? 0) / pageSize))}
          </span>
          <div className="flex gap-2">
            <Button
              variant="secondary"
              size="sm"
              disabled={page <= 1}
              onClick={() => setPage(page - 1)}
            >
              {t('common.prev')}
            </Button>
            <Button
              variant="secondary"
              size="sm"
              disabled={page * pageSize >= (data.total ?? 0)}
              onClick={() => setPage(page + 1)}
            >
              {t('common.next')}
            </Button>
          </div>
        </div>
      )}
    </div>
  );
}
