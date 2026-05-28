/**
 * Evaluation Console — /{tid}/admin/evaluation (ADR-016 §3.7 + ADR-009 §7 + ADR-017 §16).
 *
 * dataset 목록 + run + jobs + gate result + promote.
 */
'use client';

import { useParams } from 'next/navigation';
import { useState } from 'react';
import useSWR, { mutate } from 'swr';
import Card from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import {
  getEvalJob,
  listEvalDatasets,
  listEvalJobs,
  promoteEvalJob,
  runEvalJob,
} from '@/lib/api';
import type { EvalDataset, EvalJob, EvalJobListResult } from '@/lib/types';

const STATUS_COLOR: Record<EvalJob['status'], string> = {
  pending: 'bg-gray-100 text-gray-700 dark:bg-slate-700 dark:text-slate-200',
  running: 'bg-blue-100 text-blue-700 dark:bg-blue-900/30 dark:text-blue-300',
  completed: 'bg-green-100 text-green-700 dark:bg-green-900/30 dark:text-green-300',
  failed: 'bg-red-100 text-red-700 dark:bg-red-900/30 dark:text-red-300',
  promoted: 'bg-purple-100 text-purple-700 dark:bg-purple-900/30 dark:text-purple-300',
};

export default function EvaluationPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const [selectedDataset, setSelectedDataset] = useState('');
  const [running, setRunning] = useState(false);
  const [selectedJob, setSelectedJob] = useState<EvalJob | null>(null);

  const { data: datasets } = useSWR<{ items: EvalDataset[] }>(
    domainId ? `eval-datasets:${domainId}` : null,
    () => listEvalDatasets(domainId),
  );

  const swrKey = `eval-jobs:${domainId}`;
  const { data: jobs } = useSWR<EvalJobListResult>(
    domainId ? swrKey : null,
    () => listEvalJobs(domainId, { page_size: 30 }),
    { refreshInterval: 10000 },
  );

  const handleRun = async () => {
    if (!selectedDataset) {
      alert('dataset 선택');
      return;
    }
    setRunning(true);
    try {
      await runEvalJob(domainId, { dataset_name: selectedDataset });
      void mutate(swrKey);
    } catch (e) {
      alert(`run 실패: ${e instanceof Error ? e.message : ''}`);
    } finally {
      setRunning(false);
    }
  };

  const handleSelectJob = async (jobId: string) => {
    try {
      const j = await getEvalJob(domainId, jobId);
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
      await promoteEvalJob(domainId, selectedJob.job_id, { target, version });
      alert('promoted');
      void mutate(swrKey);
      void handleSelectJob(selectedJob.job_id);
    } catch (e) {
      alert(`promote 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4 text-gray-900 dark:text-slate-100">Evaluation Console</h1>

      <Card className="mb-6">
        <h2 className="font-bold mb-2 text-gray-900 dark:text-slate-100">새 평가 실행</h2>
        <div className="flex gap-2 items-center text-sm">
          <select
            value={selectedDataset}
            onChange={(e) => setSelectedDataset(e.target.value)}
            className="px-3 py-1 border rounded"
          >
            <option value="">dataset 선택</option>
            {(datasets?.items ?? []).map((d) => (
              <option key={d.name} value={d.name}>
                {d.name} ({d.case_count} cases, {d.source})
              </option>
            ))}
          </select>
          <Button onClick={handleRun} disabled={running || !selectedDataset}>
            ▶ Run
          </Button>
        </div>
      </Card>

      <div className="grid grid-cols-2 gap-6">
        <section>
          <h2 className="font-bold mb-2 text-gray-900 dark:text-slate-100">최근 jobs</h2>
          <Card padded={false} className="overflow-hidden">
            <table className="w-full text-sm border-collapse">
              <thead>
                <tr className="border-b border-gray-200 dark:border-slate-700 bg-gray-50 dark:bg-slate-900/50 text-left text-gray-500 dark:text-slate-400">
                  <th className="p-2">job_id</th>
                  <th className="p-2">dataset</th>
                  <th className="p-2">status</th>
                  <th className="p-2">완료 시각</th>
                </tr>
              </thead>
              <tbody>
                {(jobs?.items ?? []).map((j) => (
                  <tr
                    key={j.job_id}
                    className={`border-b border-gray-100 dark:border-slate-700/60 hover:bg-gray-50 dark:hover:bg-slate-700/40 cursor-pointer ${
                      selectedJob?.job_id === j.job_id ? 'bg-blue-50 dark:bg-blue-900/20' : ''
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
          </Card>
        </section>

        <section>
          <h2 className="font-bold mb-2 text-gray-900 dark:text-slate-100">선택된 job</h2>
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
                  <pre className="text-xs bg-gray-50 dark:bg-slate-900/50 p-2 mt-1">
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
                      <tr className="bg-gray-50 dark:bg-slate-900/50">
                        <th className="p-1 text-left">metric</th>
                        <th className="p-1 text-left">value</th>
                        <th className="p-1 text-left">threshold</th>
                        <th className="p-1">passed</th>
                      </tr>
                    </thead>
                    <tbody>
                      {Object.entries(selectedJob.gate_result.metrics).map(
                        ([name, m]) => (
                          <tr key={name} className="border-b border-gray-100 dark:border-slate-700/60">
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
                  <Button onClick={handlePromote} size="sm">
                    🚀 promote
                  </Button>
                )}
              {selectedJob.status === 'promoted' && (
                <p className="text-purple-700 dark:text-purple-300 text-sm">
                  promoted ({selectedJob.promotion_target}/{selectedJob.promotion_version}) at{' '}
                  {selectedJob.promoted_at
                    ? new Date(selectedJob.promoted_at).toLocaleString('ko-KR')
                    : ''}
                </p>
              )}
            </div>
          ) : (
            <p className="text-sm text-gray-400 dark:text-slate-500">job을 선택하세요.</p>
          )}
        </section>
      </div>
    </div>
  );
}
