/**
 * Evaluation Console — /{tid}/admin/evaluation (ADR-016 §3.7 + ADR-009 §7 + ADR-017 §16).
 *
 * dataset 목록 + run + jobs + gate result + promote.
 */
'use client';

import { useParams } from 'next/navigation';
import { useState } from 'react';
import useSWR, { mutate } from 'swr';
import {
  getEvalJob,
  listEvalDatasets,
  listEvalJobs,
  promoteEvalJob,
  runEvalJob,
} from '@/lib/api';
import type { EvalDataset, EvalJob, EvalJobListResult } from '@/lib/types';

const STATUS_COLOR: Record<EvalJob['status'], string> = {
  pending: 'bg-gray-100 text-gray-700',
  running: 'bg-blue-100 text-blue-700',
  completed: 'bg-green-100 text-green-700',
  failed: 'bg-red-100 text-red-700',
  promoted: 'bg-purple-100 text-purple-700',
};

export default function EvaluationPage() {
  const params = useParams<{ tenantId: string }>();
  const tenantId = params.tenantId;
  const [selectedDataset, setSelectedDataset] = useState('');
  const [running, setRunning] = useState(false);
  const [selectedJob, setSelectedJob] = useState<EvalJob | null>(null);

  const { data: datasets } = useSWR<{ items: EvalDataset[] }>(
    tenantId ? `eval-datasets:${tenantId}` : null,
    () => listEvalDatasets(tenantId),
  );

  const swrKey = `eval-jobs:${tenantId}`;
  const { data: jobs } = useSWR<EvalJobListResult>(
    tenantId ? swrKey : null,
    () => listEvalJobs(tenantId, { page_size: 30 }),
    { refreshInterval: 10000 },
  );

  const handleRun = async () => {
    if (!selectedDataset) {
      alert('dataset 선택');
      return;
    }
    setRunning(true);
    try {
      await runEvalJob(tenantId, { dataset_name: selectedDataset });
      void mutate(swrKey);
    } catch (e) {
      alert(`run 실패: ${e instanceof Error ? e.message : ''}`);
    } finally {
      setRunning(false);
    }
  };

  const handleSelectJob = async (jobId: string) => {
    try {
      const j = await getEvalJob(tenantId, jobId);
      setSelectedJob(j);
    } catch {
      // ignore
    }
  };

  const handlePromote = async () => {
    if (!selectedJob) return;
    const target = prompt('promotion target (예: prompt, lora):');
    if (!target) return;
    const version = prompt('promotion version:');
    if (!version) return;
    try {
      await promoteEvalJob(tenantId, selectedJob.job_id, { target, version });
      alert('promoted');
      void mutate(swrKey);
      void handleSelectJob(selectedJob.job_id);
    } catch (e) {
      alert(`promote 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4">Evaluation Console</h1>

      <section className="mb-6 border rounded p-4">
        <h2 className="font-bold mb-2">새 평가 실행</h2>
        <div className="flex gap-2 items-center text-sm">
          <select
            value={selectedDataset}
            onChange={(e) => setSelectedDataset(e.target.value)}
            className="px-3 py-1 border rounded"
          >
            <option value="">dataset 선택</option>
            {datasets?.items.map((d) => (
              <option key={d.name} value={d.name}>
                {d.name} ({d.case_count} cases, {d.source})
              </option>
            ))}
          </select>
          <button
            onClick={handleRun}
            disabled={running || !selectedDataset}
            className="px-3 py-1 bg-blue-600 text-white rounded disabled:bg-gray-400"
          >
            ▶ Run
          </button>
        </div>
      </section>

      <div className="grid grid-cols-2 gap-6">
        <section>
          <h2 className="font-bold mb-2">최근 jobs</h2>
          <table className="w-full text-sm border-collapse">
            <thead>
              <tr className="border-b bg-gray-50 text-left">
                <th className="p-2">job_id</th>
                <th className="p-2">dataset</th>
                <th className="p-2">status</th>
                <th className="p-2">완료 시각</th>
              </tr>
            </thead>
            <tbody>
              {jobs?.items.map((j) => (
                <tr
                  key={j.job_id}
                  className={`border-b hover:bg-gray-50 cursor-pointer ${
                    selectedJob?.job_id === j.job_id ? 'bg-blue-50' : ''
                  }`}
                  onClick={() => void handleSelectJob(j.job_id)}
                >
                  <td className="p-2 font-mono text-xs">{j.job_id.slice(0, 12)}…</td>
                  <td className="p-2 text-xs">{j.dataset_name}</td>
                  <td className="p-2">
                    <span className={`px-2 py-0.5 rounded text-xs ${STATUS_COLOR[j.status]}`}>
                      {j.status}
                    </span>
                  </td>
                  <td className="p-2 text-xs">
                    {j.finished_at
                      ? new Date(j.finished_at).toLocaleString('ko-KR')
                      : '-'}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section>
          <h2 className="font-bold mb-2">선택된 job</h2>
          {selectedJob ? (
            <div className="text-sm space-y-2">
              <p>
                <b>status:</b>{' '}
                <span
                  className={`px-2 py-0.5 rounded text-xs ${STATUS_COLOR[selectedJob.status]}`}
                >
                  {selectedJob.status}
                </span>
              </p>
              {selectedJob.summary && (
                <details open>
                  <summary className="font-bold cursor-pointer">summary</summary>
                  <pre className="text-xs bg-gray-50 p-2 mt-1">
                    {JSON.stringify(selectedJob.summary, null, 2)}
                  </pre>
                </details>
              )}
              {selectedJob.gate_result && (
                <details open>
                  <summary className="font-bold cursor-pointer">
                    gate_result {selectedJob.gate_result.passed ? '✓ PASSED' : '✗ FAILED'}
                  </summary>
                  <table className="text-xs mt-1 w-full">
                    <thead>
                      <tr className="bg-gray-50">
                        <th className="p-1 text-left">metric</th>
                        <th className="p-1 text-left">value</th>
                        <th className="p-1 text-left">threshold</th>
                        <th className="p-1">passed</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(selectedJob.gate_result.metrics).map(
                        ([name, m]) => (
                          <tr key={name} className="border-b">
                            <td className="p-1">{name}</td>
                            <td className="p-1">{m.value.toFixed(4)}</td>
                            <td className="p-1">{m.threshold.toFixed(4)}</td>
                            <td className="p-1">{m.passed ? '✓' : '✗'}</td>
                          </tr>
                        ),
                      )}
                    </tbody>
                  </table>
                </details>
              )}
              {selectedJob.status === 'completed' &&
                selectedJob.gate_result?.passed && (
                  <button
                    onClick={handlePromote}
                    className="px-3 py-1 bg-purple-600 text-white rounded text-sm"
                  >
                    🚀 promote
                  </button>
                )}
              {selectedJob.status === 'promoted' && (
                <p className="text-purple-700 text-sm">
                  promoted ({selectedJob.promotion_target}/{selectedJob.promotion_version}) at{' '}
                  {selectedJob.promoted_at
                    ? new Date(selectedJob.promoted_at).toLocaleString('ko-KR')
                    : ''}
                </p>
              )}
            </div>
          ) : (
            <p className="text-sm text-gray-400">job을 선택하세요.</p>
          )}
        </section>
      </div>
    </div>
  );
}
