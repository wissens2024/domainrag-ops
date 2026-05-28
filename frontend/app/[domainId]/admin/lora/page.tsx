/**
 * LoRA Registry — /{tid}/admin/lora (ADR-016 §3.6 + ADR-013 + ADR-017 §14).
 *
 * 목록 + upload(multipart) + activate / retire / delete.
 */
'use client';

import { useParams } from 'next/navigation';
import { useState } from 'react';
import useSWR, { mutate } from 'swr';
import Card from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import Badge from '@/components/ui/Badge';
import {
  activateLoRA,
  deleteLoRA,
  listLoRA,
  retireLoRA,
  uploadLoRA,
} from '@/lib/api';
import type { AdapterRecord, LoRAStatus } from '@/lib/types';

const STATUS_TONE: Record<LoRAStatus, 'warn' | 'success' | 'neutral'> = {
  registered: 'warn',
  active: 'success',
  retired: 'neutral',
};

export default function LoRARegistryPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const [statusFilter, setStatusFilter] = useState<LoRAStatus | ''>('');
  const [uploading, setUploading] = useState(false);
  const [uploadForm, setUploadForm] = useState({
    adapter_id: '',
    version: 'v1',
    base_model: 'Qwen2.5-7B',
  });
  const [uploadFile, setUploadFile] = useState<File | null>(null);

  const swrKey = `lora:${domainId}:${statusFilter}`;
  const { data, isLoading } = useSWR<{ items: AdapterRecord[]; total: number }>(
    domainId ? swrKey : null,
    () => listLoRA(domainId, statusFilter || undefined),
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
      await uploadLoRA(domainId, uploadFile, uploadForm);
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
      await activateLoRA(domainId, aid);
      void mutate(swrKey);
    } catch (e) {
      alert(`activate 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };
  const handleRetire = async (aid: string) => {
    if (!confirm(`adapter ${aid}을 retire 하시겠습니까?`)) return;
    try {
      await retireLoRA(domainId, aid);
      void mutate(swrKey);
    } catch (e) {
      alert(`retire 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };
  const handleDelete = async (aid: string) => {
    if (!confirm(`adapter ${aid}을 영구 삭제? (active면 거부됨)`)) return;
    try {
      await deleteLoRA(domainId, aid);
      void mutate(swrKey);
    } catch (e) {
      alert(`delete 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4 text-gray-900 dark:text-slate-100">LoRA Registry</h1>

      <Card className="mb-6">
      <form
        onSubmit={handleUpload}
        className="grid grid-cols-12 gap-2 items-end text-sm"
      >
        <div className="col-span-3">
          <label className="block text-xs text-gray-500 dark:text-slate-400">adapter_id</label>
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
          <label className="block text-xs text-gray-500 dark:text-slate-400">version</label>
          <input
            value={uploadForm.version}
            onChange={(e) =>
              setUploadForm({ ...uploadForm, version: e.target.value })
            }
            className="w-full px-2 py-1 border rounded"
          />
        </div>
        <div className="col-span-3">
          <label className="block text-xs text-gray-500 dark:text-slate-400">base_model</label>
          <input
            value={uploadForm.base_model}
            onChange={(e) =>
              setUploadForm({ ...uploadForm, base_model: e.target.value })
            }
            className="w-full px-2 py-1 border rounded"
          />
        </div>
        <div className="col-span-3">
          <label className="block text-xs text-gray-500 dark:text-slate-400">weights file</label>
          <input
            type="file"
            onChange={(e) => setUploadFile(e.target.files?.[0] ?? null)}
            className="text-xs"
          />
        </div>
        <Button type="submit" disabled={uploading} size="sm" className="col-span-1">
          {uploading ? '...' : '업로드'}
        </Button>
      </form>
      </Card>

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

      {isLoading && <p className="text-gray-500 dark:text-slate-400">로딩 중...</p>}

      {data && (
        <Card padded={false} className="overflow-hidden">
        <table className="w-full text-sm border-collapse">
          <thead>
            <tr className="border-b border-gray-200 dark:border-slate-700 bg-gray-50 dark:bg-slate-900/50 text-left text-gray-500 dark:text-slate-400">
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
              <tr key={r.adapter_id} className="border-b border-gray-100 dark:border-slate-700/60 hover:bg-gray-50 dark:hover:bg-slate-700/40">
                <td className="p-2 font-mono text-xs">{r.adapter_id}</td>
                <td className="p-2 text-xs">{r.version || '-'}</td>
                <td className="p-2 text-xs">{r.base_model || '-'}</td>
                <td className="p-2">
                  <Badge tone={STATUS_TONE[r.status]}>{r.status}</Badge>
                </td>
                <td className="p-2 font-mono text-xs truncate max-w-xs">
                  {r.keyhub_secret_ref || '-'}
                </td>
                <td className="p-2 text-xs">
                  {new Date(r.created_at).toLocaleString('ko-KR')}
                </td>
                <td className="p-2 text-xs space-x-1 whitespace-nowrap">
                  {r.status === 'registered' && (
                    <Button variant="secondary" size="sm" onClick={() => void handleActivate(r.adapter_id)}>
                      activate
                    </Button>
                  )}
                  {(r.status === 'registered' || r.status === 'active') && (
                    <Button variant="secondary" size="sm" onClick={() => void handleRetire(r.adapter_id)}>
                      retire
                    </Button>
                  )}
                  {r.status !== 'active' && (
                    <Button variant="danger" size="sm" onClick={() => void handleDelete(r.adapter_id)}>
                      delete
                    </Button>
                  )}
                </td>
              </tr>
            ))}
            {data.items.length === 0 && (
              <tr>
                <td colSpan={7} className="p-4 text-center text-gray-500 dark:text-slate-400">
                  adapter가 없습니다.
                </td>
              </tr>
            )}
          </tbody>
        </table>
        </Card>
      )}
    </div>
  );
}
