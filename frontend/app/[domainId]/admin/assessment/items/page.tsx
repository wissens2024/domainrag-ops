/**
 * Assessment Item Bank — /{tid}/admin/assessment/items (ADR-014 + ADR-016 §3 + ADR-017 §17).
 *
 * 목록 + 검색·필터 + status 칩 + approve 액션.
 */
'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useState } from 'react';
import useSWR, { mutate } from 'swr';
import Card from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import Badge from '@/components/ui/Badge';
import {
  approveAssessmentItem,
  listAssessmentItems,
} from '@/lib/api';
import type {
  AssessmentListResult,
  AssessmentQualityStatus,
} from '@/lib/types';

const STATUS_TONE: Record<AssessmentQualityStatus, 'neutral' | 'info' | 'success'> = {
  draft: 'neutral',
  reviewed: 'info',
  approved: 'success',
  retired: 'neutral',
};

export default function ItemBankPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const [filters, setFilters] = useState({
    keyword: '',
    subject: '',
    difficulty: '',
    quality_status: '' as AssessmentQualityStatus | '',
  });
  const [page, setPage] = useState(1);
  const pageSize = 20;

  const swrKey = `assessment:${domainId}:${JSON.stringify(filters)}:${page}`;
  const { data, isLoading } = useSWR<AssessmentListResult>(
    domainId ? swrKey : null,
    () =>
      listAssessmentItems(domainId, {
        keyword: filters.keyword || undefined,
        subject: filters.subject || undefined,
        difficulty: filters.difficulty || undefined,
        quality_status: filters.quality_status || undefined,
        page,
        page_size: pageSize,
      }),
  );

  const handleApprove = async (itemId: string) => {
    if (!confirm('approved로 전이?')) return;
    try {
      await approveAssessmentItem(domainId, itemId);
      void mutate(swrKey);
    } catch (e) {
      alert(`승인 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-4">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-slate-100">Item Bank</h1>
        <div className="flex gap-2 text-sm">
          <Link
            href={`/${domainId}/admin/assessment/workbench`}
            className="px-3 py-1.5 text-sm font-medium rounded-lg border border-gray-300 dark:border-slate-600 text-gray-900 dark:text-slate-100 hover:bg-gray-50 dark:hover:bg-slate-700"
          >
            Workbench
          </Link>
          <Link
            href={`/${domainId}/admin/assessment/review-queue`}
            className="px-3 py-1.5 text-sm font-medium rounded-lg border border-gray-300 dark:border-slate-600 text-gray-900 dark:text-slate-100 hover:bg-gray-50 dark:hover:bg-slate-700"
          >
            Review Queue
          </Link>
        </div>
      </div>

      <div className="grid grid-cols-5 gap-2 mb-4 text-sm">
        <input
          placeholder="keyword"
          value={filters.keyword}
          onChange={(e) => {
            setFilters({ ...filters, keyword: e.target.value });
            setPage(1);
          }}
          className="px-2 py-1 border rounded col-span-2"
        />
        <input
          placeholder="subject"
          value={filters.subject}
          onChange={(e) => {
            setFilters({ ...filters, subject: e.target.value });
            setPage(1);
          }}
          className="px-2 py-1 border rounded"
        />
        <select
          value={filters.difficulty}
          onChange={(e) => {
            setFilters({ ...filters, difficulty: e.target.value });
            setPage(1);
          }}
          className="px-2 py-1 border rounded"
        >
          <option value="">모든 난이도</option>
          <option value="easy">easy</option>
          <option value="medium">medium</option>
          <option value="hard">hard</option>
        </select>
        <select
          value={filters.quality_status}
          onChange={(e) => {
            setFilters({
              ...filters,
              quality_status: e.target.value as AssessmentQualityStatus | '',
            });
            setPage(1);
          }}
          className="px-2 py-1 border rounded"
        >
          <option value="">모든 status</option>
          <option value="draft">draft</option>
          <option value="reviewed">reviewed</option>
          <option value="approved">approved</option>
          <option value="retired">retired (비활성)</option>
        </select>
      </div>

      {isLoading && <p className="text-gray-500 dark:text-slate-400">로딩...</p>}
      {data && (
        <>
          <Card padded={false} className="overflow-hidden">
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b border-gray-200 dark:border-slate-700 bg-gray-50 dark:bg-slate-900/50 text-left text-gray-500 dark:text-slate-400">
                <th className="p-2">item_id</th>
                <th className="p-2">문제</th>
                <th className="p-2">subject</th>
                <th className="p-2">난이도</th>
                <th className="p-2">status</th>
                <th className="p-2">score</th>
                <th className="p-2">used</th>
                <th className="p-2">액션</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((it) => (
                <tr key={it.item_id} className="border-b border-gray-100 dark:border-slate-700/60 hover:bg-gray-50 dark:hover:bg-slate-700/40">
                  <td className="p-2 font-mono text-xs">{it.item_id.slice(0, 12)}…</td>
                  <td className="p-2 text-xs truncate max-w-md">
                    {it.question_text}
                  </td>
                  <td className="p-2 text-xs">{it.subject}</td>
                  <td className="p-2 text-xs">{it.difficulty || '-'}</td>
                  <td className="p-2">
                    <Badge tone={STATUS_TONE[it.quality_status]}>
                      {it.quality_status === 'retired' ? '비활성' : it.quality_status}
                    </Badge>
                  </td>
                  <td className="p-2 text-xs">
                    {it.quality_score?.toFixed(2) || '-'}
                  </td>
                  <td className="p-2 text-xs">{it.used_count}</td>
                  <td className="p-2 text-xs">
                    {(it.quality_status === 'draft' ||
                      it.quality_status === 'reviewed') && (
                      <Button variant="secondary" size="sm" onClick={() => void handleApprove(it.item_id)}>
                        approve
                      </Button>
                    )}
                  </td>
                </tr>
              ))}
              {data.items.length === 0 && (
                <tr>
                  <td colSpan={8} className="p-4 text-center text-gray-500 dark:text-slate-400">
                    조건에 맞는 item이 없습니다.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          </Card>

          <div className="mt-4 flex justify-between items-center text-sm text-gray-600 dark:text-slate-300">
            <span>총 {data.total}건 · {page}쪽</span>
            <div className="flex gap-2">
              <Button variant="secondary" size="sm" disabled={page <= 1} onClick={() => setPage(page - 1)}>
                이전
              </Button>
              <Button variant="secondary" size="sm" disabled={page * pageSize >= (data.total ?? 0)} onClick={() => setPage(page + 1)}>
                다음
              </Button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
