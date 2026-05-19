/**
 * LoRA Registry — /{tid}/admin/lora (ADR-016 §3.6 + ADR-013 + ADR-017 §14).
 *
 * 목록 + upload(multipart) + activate / retire / delete.
 */
'use client';

import { useParams } from 'next/navigation';
import { useState } from 'react';
import useSWR, { mutate } from 'swr';
import {
  activateLoRA,
  deleteLoRA,
  listLoRA,
  retireLoRA,
  uploadLoRA,
} from '@/lib/api';
import type { AdapterRecord, LoRAStatus } from '@/lib/types';

const STATUS_COLOR: Record<LoRAStatus, string> = {
  registered: 'bg-yellow-100 text-yellow-700',
  active: 'bg-green-100 text-green-700',
  retired: 'bg-gray-200 text-gray-700',
};

export default function LoRARegistryPage() {
  const params = useParams<{ tenantId: string }>();
  const tenantId = params.tenantId;
  const [statusFilter, setStatusFilter] = useState<LoRAStatus | ''>('');
  const [uploading, setUploading] = useState(false);
  const [uploadForm, setUploadForm] = useState({
    adapter_id: '',
    version: 'v1',
    base_model: 'Qwen2.5-7B',
  });
  const [uploadFile, setUploadFile] = useState<File | null>(null);

  const swrKey = `lora:${tenantId}:${statusFilter}`;
  const { data, isLoading } = useSWR<{ items: AdapterRecord[]; total: number }>(
    tenantId ? swrKey : null,
    () => listLoRA(tenantId, statusFilter || undefined),
  );

  const handleUpload = async (e: React.FormEvent) => {
    e.preventDefault();
    if (!uploadFile) {
      alert('weights 파일을 선택해주세요');
      return;
    }
    if (!uploadForm.adapter_id.trim()) {
      alert('adapter_id 입력 필요');
      return;
    }
    setUploading(true);
    try {
      await uploadLoRA(tenantId, uploadFile, uploadForm);
      setUploadFile(null);
      setUploadForm({ adapter_id: '', version: 'v1', base_model: 'Qwen2.5-7B' });
      void mutate(swrKey);
      alert('업로드 완료');
    } catch (e) {
      alert(`업로드 실패: ${e instanceof Error ? e.message : ''}`);
    } finally {
      setUploading(false);
    }
  };

  const handleActivate = async (aid: string) => {
    try {
      await activateLoRA(tenantId, aid);
      void mutate(swrKey);
    } catch (e) {
      alert(`activate 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };
  const handleRetire = async (aid: string) => {
    if (!confirm(`adapter ${aid}을 retire 하시겠습니까?`)) return;
    try {
      await retireLoRA(tenantId, aid);
      void mutate(swrKey);
    } catch (e) {
      alert(`retire 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };
  const handleDelete = async (aid: string) => {
    if (!confirm(`adapter ${aid}을 영구 삭제? (active면 거부됨)`)) return;
    try {
      await deleteLoRA(tenantId, aid);
      void mutate(swrKey);
    } catch (e) {
      alert(`delete 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">LoRA Registry</h1>

      <form
        onSubmit={handleUpload}
        className="border rounded p-4 mb-6 grid grid-cols-12 gap-2 items-end text-sm"
      >
        <div className="col-span-3">
          <label className="block text-xs text-gray-500">adapter_id</label>
          <input
            value={uploadForm.adapter_id}
            onChange={(e) =>
              setUploadForm({ ...uploadForm, adapter_id: e.target.value })
            }
            className="w-full px-2 py-1 border rounded"
            placeholder="security-policy-v1"
          />
        </div>
        <div className="col-span-2">
          <label className="block text-xs text-gray-500">version</label>
          <input
            value={uploadForm.version}
            onChange={(e) =>
              setUploadForm({ ...uploadForm, version: e.target.value })
            }
            className="w-full px-2 py-1 border rounded"
          />
        </div>
        <div className="col-span-3">
          <label className="block text-xs text-gray-500">base_model</label>
          <input
            value={uploadForm.base_model}
            onChange={(e) =>
              setUploadForm({ ...uploadForm, base_model: e.target.value })
            }
            className="w-full px-2 py-1 border rounded"
          />
        </div>
        <div className="col-span-3">
          <label className="block text-xs text-gray-500">weights file</label>
          <input
            type="file"
            onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
            className="text-xs"
          />
        </div>
        <button
          type="submit"
          disabled={uploading}
          className="col-span-1 px-3 py-1 bg-blue-600 text-white rounded disabled:bg-gray-400"
        >
          {uploading ? '...' : '업로드'}
        </button>
      </form>

      <div className="flex gap-2 mb-3 text-sm">
        <select
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value as LoRAStatus | '')}
          className="px-3 py-1 border rounded"
        >
          <option value="">모든 status</option>
          <option value="registered">registered</option>
          <option value="active">active</option>
          <option value="retired">retired</option>
        </select>
      </div>

      {isLoading && <p>로딩 중...</p>}

      {data && (
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="border-b bg-gray-50 text-left">
              <th className="p-2">adapter_id</th>
              <th className="p-2">version</th>
              <th className="p-2">base_model</th>
              <th className="p-2">status</th>
              <th className="p-2">KeyHub ref</th>
              <th className="p-2">생성</th>
              <th className="p-2">액션</th>
            </tr>
          </thead>
          <tbody>
            {data.items.map((r) => (
              <tr key={r.adapter_id} className="border-b hover:bg-gray-50">
                <td className="p-2 font-mono text-xs">{r.adapter_id}</td>
                <td className="p-2 text-xs">{r.version || '-'}</td>
                <td className="p-2 text-xs">{r.base_model || '-'}</td>
                <td className="p-2">
                  <span className={`px-2 py-0.5 rounded text-xs ${STATUS_COLOR[r.status]}`}>
                    {r.status}
                  </span>
                </td>
                <td className="p-2 font-mono text-xs truncate max-w-xs">
                  {r.keyhub_secret_ref || '-'}
                </td>
                <td className="p-2 text-xs">
                  {new Date(r.created_at).toLocaleString('ko-KR')}
                </td>
                <td className="p-2 text-xs space-x-1">
                  {r.status === 'registered' && (
                    <button
                      onClick={() => void handleActivate(r.adapter_id)}
                      className="px-1 border rounded"
                    >
                      activate
                    </button>
                  )}
                  {(r.status === 'registered' || r.status === 'active') && (
                    <button
                      onClick={() => void handleRetire(r.adapter_id)}
                      className="px-1 border rounded"
                    >
                      retire
                    </button>
                  )}
                  {r.status !== 'active' && (
                    <button
                      onClick={() => void handleDelete(r.adapter_id)}
                      className="px-1 border rounded text-red-600"
                    >
                      delete
                    </button>
                  )}
                </td>
              </tr>
            ))}
            {data.items.length === 0 && (
              <tr>
                <td colSpan={7} className="p-4 text-center text-gray-500">
                  adapter가 없습니다.
                </td>
              </tr>
            )}
          </tbody>
        </table>
      )}
    </div>
  );
}
