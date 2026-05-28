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
import {
  approveAssessmentItem,
  listAssessmentItems,
} from '@/lib/api';
import type {
  AssessmentListResult,
  AssessmentQualityStatus,
} from '@/lib/types';

const STATUS_COLOR: Record<AssessmentQualityStatus, string> = {
  draft: 'bg-gray-100 text-gray-700',
  reviewed: 'bg-blue-100 text-blue-700',
  approved: 'bg-green-100 text-green-700',
  retired: 'bg-gray-200 text-gray-500',
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
        <h1 className="text-2xl font-bold">Item Bank</h1>
        <div className="flex gap-2 text-sm">
          <Link
            href={`/${domainId}/admin/assessment/workbench`}
            className="px-3 py-1 border rounded"
          >
            Workbench
          </Link>
          <Link
            href={`/${domainId}/admin/assessment/review-queue`}
            className="px-3 py-1 border rounded"
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

      {isLoading && <p>로딩...</p>}
      {data && (
        <>
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b bg-gray-50 text-left">
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
                <tr key={it.item_id} className="border-b hover:bg-gray-50">
                  <td className="p-2 font-mono text-xs">{it.item_id.slice(0, 12)}…</td>
                  <td className="p-2 text-xs truncate max-w-md">
                    {it.question_text}
                  </td>
                  <td className="p-2 text-xs">{it.subject}</td>
                  <td className="p-2 text-xs">{it.difficulty || '-'}</td>
                  <td className="p-2">
                    <span
                      className={`px-2 py-0.5 rounded text-xs ${STATUS_COLOR[it.quality_status]}`}
                    >
                      {it.quality_status === 'retired' ? '비활성' : it.quality_status}
                    </span>
                  </td>
                  <td className="p-2 text-xs">
                    {it.quality_score?.toFixed(2) || '-'}
                  </td>
                  <td className="p-2 text-xs">{it.used_count}</td>
                  <td className="p-2 text-xs">
                    {(it.quality_status === 'draft' ||
                      it.quality_status === 'reviewed') && (
                      <button
                        onClick={() => void handleApprove(it.item_id)}
                        className="px-1 border rounded"
                      >
                        approve
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {data.items.length === 0 && (
                <tr>
                  <td colSpan={8} className="p-4 text-center text-gray-500">
                    조건에 맞는 item이 없습니다.
                  </td>
                </tr>
              )}
            </tbody>
          </table>

          <div className="mt-4 flex justify-between text-sm">
            <span>총 {data.total}건 · {page}쪽</span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage(page - 1)}
                className="px-2 py-1 border rounded disabled:bg-gray-100"
              >
                이전
              </button>
              <button
                disabled={page * pageSize >= data.total}
                onClick={() => setPage(page + 1)}
                className="px-2 py-1 border rounded disabled:bg-gray-100"
              >
                다음
              </button>
            </div>
          </div>
        </>
      )}
    </div>
  );
}
