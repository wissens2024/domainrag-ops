/**
 * Document Detail — /{tid}/admin/documents/{doc_id} (ADR-016 §3.2 + ADR-017 §6.4).
 *
 * 메타데이터 표시 + chunks_summary + 하드 삭제 액션.
 */
'use client';

import Link from 'next/link';
import { useParams, useRouter } from 'next/navigation';
import { useState } from 'react';
import useSWR from 'swr';
import {
  getDocument,
  hardDeleteDocument,
  patchDocumentApproval,
  patchDocumentMetadata,
} from '@/lib/api';
import type { DocumentDetail } from '@/lib/types';

export default function DocumentDetailPage() {
  const params = useParams<{ tenantId: string; docId: string }>();
  const router = useRouter();
  const tenantId = params.tenantId;
  const docId = params.docId;
  const [version, setVersion] = useState('v1');

  const { data, error, isLoading, mutate } = useSWR<DocumentDetail>(
    tenantId && docId ? `doc:${tenantId}:${docId}:${version}` : null,
    () => getDocument(tenantId, docId, version),
  );

  const handleEditMetadata = async () => {
    if (!data) return;
    const titleNew = prompt('새 title:', data.title);
    if (titleNew === null) return;
    try {
      await patchDocumentMetadata(tenantId, docId, {
        patch: { title: titleNew },
        version,
        reason: 'admin edit',
      });
      void mutate();
    } catch (e) {
      alert(`수정 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  const handleHardDelete = async () => {
    const reason = prompt('하드 삭제 사유 (필수):');
    if (!reason) return;
    if (!confirm(`정말 ${docId} v${version}을 영구 삭제하시겠습니까?\nchunks/Qdrant/Storage 모두 삭제됩니다.`)) return;
    try {
      const result = await hardDeleteDocument(tenantId, docId, {
        reason,
        chat_logs_action: 'keep_excerpts',
        version,
      });
      alert(
        `삭제 완료\nchunks: ${result.removed_chunks}\nstorage_files: ${result.storage_files}\ndocuments: ${result.removed_documents}\naffected_chat_logs: ${result.affected_chat_logs}`,
      );
      router.push(`/${tenantId}/admin/documents`);
    } catch (e) {
      alert(`삭제 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  if (isLoading) return <div className="p-6">로딩 중...</div>;
  if (error) return <div className="p-6 text-red-600">로드 실패: {error.message}</div>;
  if (!data) return null;

  return (
    <div className="p-6 max-w-4xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold">{data.title}</h1>
        <Link
          href={`/${tenantId}/admin/documents`}
          className="text-sm text-blue-600 hover:underline"
        >
          ← 문서 목록
        </Link>
      </div>

      <div className="grid grid-cols-2 gap-4 mb-6">
        <Field label="doc_id" value={data.doc_id} mono />
        <Field label="version" value={data.version} />
        <Field label="input_type" value={data.input_type || '-'} />
        <Field label="approval_status" value={data.approval_status} />
        <Field label="department" value={data.department || '-'} />
        <Field label="doc_type" value={data.doc_type || '-'} />
        <Field label="security_level" value={data.security_level || '-'} />
        <Field label="language" value={data.language || '-'} />
        <Field label="owner" value={data.owner || '-'} />
        <Field label="tags" value={(data.tags || []).join(', ') || '-'} />
        <Field label="valid_from" value={data.valid_from || '-'} />
        <Field label="valid_until" value={data.valid_until || '-'} />
        <Field
          label="chunk_count"
          value={`${data.chunk_count} (archived: ${data.chunks_summary?.archived || 0}, failed: ${data.chunks_summary?.failed || 0})`}
        />
        <Field label="object_storage_path" value={data.object_storage_path} mono />
        <Field
          label="created_at"
          value={new Date(data.created_at).toLocaleString('ko-KR')}
        />
        <Field
          label="last_indexed_at"
          value={
            data.last_indexed_at
              ? new Date(data.last_indexed_at).toLocaleString('ko-KR')
              : '-'
          }
        />
      </div>

      <div className="mb-6">
        <h2 className="font-bold mb-2">domain metadata</h2>
        <pre className="bg-gray-50 border rounded p-3 text-xs overflow-x-auto">
          {JSON.stringify(data.metadata || {}, null, 2)}
        </pre>
      </div>

      <div className="flex gap-2">
        <button onClick={handleEditMetadata} className="px-3 py-2 border rounded text-sm">
          메타 수정
        </button>
        <button
          onClick={async () => {
            try {
              await patchDocumentApproval(tenantId, docId, {
                status: 'approved',
                version,
              });
              void mutate();
            } catch (e) {
              alert(`승인 실패: ${e instanceof Error ? e.message : ''}`);
            }
          }}
          className="px-3 py-2 border rounded text-sm"
        >
          승인
        </button>
        <button
          onClick={async () => {
            try {
              await patchDocumentApproval(tenantId, docId, {
                status: 'archived',
                version,
              });
              void mutate();
            } catch (e) {
              alert(`비활성 실패: ${e instanceof Error ? e.message : ''}`);
            }
          }}
          className="px-3 py-2 border rounded text-sm"
        >
          비활성
        </button>
        <button
          onClick={handleHardDelete}
          className="px-3 py-2 border rounded text-sm bg-red-50 text-red-700"
        >
          하드 삭제
        </button>
      </div>
    </div>
  );
}

function Field({
  label,
  value,
  mono,
}: {
  label: string;
  value: string;
  mono?: boolean;
}) {
  return (
    <div>
      <p className="text-xs text-gray-500">{label}</p>
      <p className={`text-sm ${mono ? 'font-mono' : ''}`}>{value}</p>
    </div>
  );
}
