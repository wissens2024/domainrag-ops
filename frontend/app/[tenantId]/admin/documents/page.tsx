/**
 * Document Management — /{tid}/admin/documents (ADR-016 §3.2 + ADR-017 §6).
 *
 * 목록 + 검색·필터·페이징 + reindex(4 mode) + approval patch + upload 진입점.
 */
'use client';

import Link from 'next/link';
import { useParams } from 'next/navigation';
import { useState } from 'react';
import useSWR, { mutate } from 'swr';
import {
  listDocuments,
  patchDocumentApproval,
  reindexDocument,
} from '@/lib/api';
import type {
  ApprovalStatus,
  DocumentListResult,
  ReindexMode,
} from '@/lib/types';

const STATUS_LABEL: Record<ApprovalStatus, string> = {
  pending: '대기',
  approved: '승인',
  archived: '비활성',
};

const STATUS_COLOR: Record<ApprovalStatus, string> = {
  pending: 'bg-yellow-100 text-yellow-700',
  approved: 'bg-green-100 text-green-700',
  archived: 'bg-gray-100 text-gray-500',
};

const REINDEX_OPTIONS: { mode: ReindexMode; label: string; desc: string }[] = [
  { mode: 'full', label: 'FULL', desc: '파싱부터 임베딩까지 전체 재처리' },
  { mode: 'chunk_re_split', label: 'CHUNK_RE_SPLIT', desc: 'chunking 부터 재처리' },
  { mode: 'embedding_only', label: 'EMBEDDING_ONLY', desc: 'chunks 보존 + vectors 재계산' },
  { mode: 'parser_only', label: 'PARSER_ONLY', desc: 'metadata만 갱신' },
];

export default function DocumentsPage() {
  const params = useParams<{ tenantId: string }>();
  const tenantId = params.tenantId;
  const [keyword, setKeyword] = useState('');
  const [approval, setApproval] = useState<ApprovalStatus | ''>('');
  const [page, setPage] = useState(1);
  const pageSize = 20;
  const [reindexFor, setReindexFor] = useState<string | null>(null);

  const swrKey = `documents:${tenantId}:${page}:${pageSize}:${keyword}:${approval}`;
  const { data, isLoading, error } = useSWR<DocumentListResult>(
    tenantId ? swrKey : null,
    () =>
      listDocuments(tenantId, {
        keyword: keyword || undefined,
        approval_status: approval || undefined,
        page,
        page_size: pageSize,
      }),
  );

  const handleReindex = async (docId: string, mode: ReindexMode) => {
    try {
      await reindexDocument(tenantId, docId, mode);
      alert(`reindex 요청됨 (mode=${mode}). 진행 상황은 인덱싱 모니터링에서.`);
      setReindexFor(null);
    } catch (e) {
      alert(`reindex 실패: ${e instanceof Error ? e.message : '알 수 없는 오류'}`);
    }
  };

  const handleApprove = async (docId: string, status: ApprovalStatus) => {
    if (!confirm(`approval_status='${status}'으로 변경?`)) return;
    try {
      await patchDocumentApproval(tenantId, docId, { status });
      void mutate(swrKey);
    } catch (e) {
      alert(`approval 변경 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6">
      <div className="flex justify-between items-center mb-4">
        <h1 className="text-2xl font-bold">문서 관리</h1>
        <Link
          href={`/${tenantId}/admin/documents/upload`}
          className="px-3 py-2 bg-blue-600 text-white rounded text-sm"
        >
          + 문서 업로드
        </Link>
      </div>

      <div className="flex gap-2 mb-4">
        <input
          value={keyword}
          onChange={(e) => setKeyword(e.target.value)}
          onKeyDown={(e) => e.key === 'Enter' && setPage(1)}
          placeholder="제목 / 부서 / tags 검색"
          className="flex-1 px-3 py-2 border rounded text-sm"
        />
        <select
          value={approval}
          onChange={(e) => {
            setApproval(e.target.value as ApprovalStatus | '');
            setPage(1);
          }}
          className="px-3 py-2 border rounded text-sm"
        >
          <option value="">모든 승인 상태</option>
          <option value="pending">대기</option>
          <option value="approved">승인</option>
          <option value="archived">비활성</option>
        </select>
      </div>

      {isLoading && <p>로딩 중...</p>}
      {error && <p className="text-red-600">목록 로드 실패: {error.message}</p>}

      {data && (
        <>
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b bg-gray-50 text-left">
                <th className="p-2">문서명</th>
                <th className="p-2">input_type</th>
                <th className="p-2">부서</th>
                <th className="p-2">보안</th>
                <th className="p-2">버전</th>
                <th className="p-2">chunk수</th>
                <th className="p-2">상태</th>
                <th className="p-2">최근 색인</th>
                <th className="p-2">액션</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((d) => (
                <tr key={d.doc_id} className="border-b hover:bg-gray-50">
                  <td className="p-2">
                    <Link
                      href={`/${tenantId}/admin/documents/${d.doc_id}`}
                      className="font-medium text-blue-600 hover:underline"
                    >
                      {d.title}
                    </Link>
                    <div className="text-xs text-gray-500">{d.doc_id}</div>
                  </td>
                  <td className="p-2 text-xs">{d.input_type || '-'}</td>
                  <td className="p-2 text-xs">{d.department || '-'}</td>
                  <td className="p-2 text-xs">{d.security_level || '-'}</td>
                  <td className="p-2 text-xs">{d.version}</td>
                  <td className="p-2 text-xs">{d.chunk_count}</td>
                  <td className="p-2">
                    <span
                      className={`px-2 py-0.5 rounded text-xs ${STATUS_COLOR[d.approval_status]}`}
                    >
                      {STATUS_LABEL[d.approval_status]}
                    </span>
                  </td>
                  <td className="p-2 text-xs">
                    {d.last_indexed_at
                      ? new Date(d.last_indexed_at).toLocaleDateString('ko-KR')
                      : '-'}
                  </td>
                  <td className="p-2 text-xs space-x-1">
                    <button
                      className="px-1 border rounded"
                      onClick={() =>
                        setReindexFor(reindexFor === d.doc_id ? null : d.doc_id)
                      }
                    >
                      재색인
                    </button>
                    {d.approval_status !== 'approved' && (
                      <button
                        className="px-1 border rounded"
                        onClick={() => handleApprove(d.doc_id, 'approved')}
                      >
                        승인
                      </button>
                    )}
                    {d.approval_status !== 'archived' && (
                      <button
                        className="px-1 border rounded"
                        onClick={() => handleApprove(d.doc_id, 'archived')}
                      >
                        비활성
                      </button>
                    )}
                  </td>
                </tr>
              ))}
              {data.items.length === 0 && (
                <tr>
                  <td colSpan={9} className="p-4 text-center text-gray-500">
                    문서가 없습니다.
                  </td>
                </tr>
              )}
            </tbody>
          </table>

          {reindexFor && (
            <div className="mt-4 p-4 border rounded bg-gray-50">
              <p className="text-sm font-bold mb-2">재색인 mode 선택 ({reindexFor})</p>
              <div className="grid grid-cols-2 gap-2">
                {REINDEX_OPTIONS.map((opt) => (
                  <button
                    key={opt.mode}
                    onClick={() => void handleReindex(reindexFor, opt.mode)}
                    className="text-left p-2 border rounded hover:bg-white"
                  >
                    <span className="font-mono text-xs font-bold">{opt.label}</span>
                    <p className="text-xs text-gray-500">{opt.desc}</p>
                  </button>
                ))}
              </div>
              <button
                onClick={() => setReindexFor(null)}
                className="mt-2 text-xs text-gray-500 hover:underline"
              >
                취소
              </button>
            </div>
          )}

          <div className="mt-4 flex justify-between items-center text-sm">
            <span>
              총 {data.total}건 · {page} / {Math.max(1, Math.ceil(data.total / pageSize))}
            </span>
            <div className="flex gap-2">
              <button
                disabled={page <= 1}
                onClick={() => setPage(page - 1)}
                className="px-2 py-1 border rounded disabled:bg-gray-100 disabled:text-gray-400"
              >
                이전
              </button>
              <button
                disabled={page * pageSize >= data.total}
                onClick={() => setPage(page + 1)}
                className="px-2 py-1 border rounded disabled:bg-gray-100 disabled:text-gray-400"
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
