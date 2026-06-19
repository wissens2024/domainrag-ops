/**
 * Generation Workbench — /{tid}/admin/assessment/workbench (ADR-014 §3·§4·§5, ADR-025 §3b·§4).
 *
 * extract / generate / hybrid / figure-reuse 4개 모드 시연 + 결과 검토.
 */
'use client';

import { useParams } from 'next/navigation';
import { useState } from 'react';
import Button from '@/components/ui/Button';
import {
  extractAssessment,
  figureReuseAssessment,
  generateAssessment,
  hybridAssessment,
} from '@/lib/api';
import type { AssessmentExtractResult, FigureReuseResult } from '@/lib/types';

type Mode = 'extract' | 'generate' | 'hybrid' | 'figure-reuse';

export default function WorkbenchPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const [mode, setMode] = useState<Mode>('extract');
  const [form, setForm] = useState({
    subject: '정보보안',
    chapter: '',
    difficulty: 'medium',
    count: 5,
    extract_ratio: 0.5,
  });
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<
    AssessmentExtractResult | FigureReuseResult | null
  >(null);
  const [figureSummary, setFigureSummary] = useState<FigureReuseResult | null>(
    null,
  );
  const [error, setError] = useState<string | null>(null);

  const handleRun = async () => {
    setRunning(true);
    setError(null);
    setResult(null);
    setFigureSummary(null);
    try {
      let r: AssessmentExtractResult | FigureReuseResult;
      if (mode === 'extract') {
        r = await extractAssessment(domainId, {
          subject: form.subject || undefined,
          chapter: form.chapter || undefined,
          count: form.count,
        });
      } else if (mode === 'generate') {
        r = await generateAssessment(domainId, {
          subject: form.subject,
          chapter: form.chapter || undefined,
          count: form.count,
          difficulty: form.difficulty,
        });
      } else if (mode === 'figure-reuse') {
        // ADR-025 §3b·§4 — figure_dependent approved 문항의 그림을 승계해 VLM이
        // 새 질문 생성. 신규 그림 합성 없음. VLM 비가동 시 degrade(생성 0).
        const fr = await figureReuseAssessment(domainId, {
          subject: form.subject,
          chapter: form.chapter || undefined,
          count: form.count,
          difficulty: form.difficulty,
        });
        setFigureSummary(fr);
        r = fr;
      } else {
        // hybrid = extract 일부 + 부족분은 generate (ADR-014). extract_ratio
        // 만큼 기존 item을 끌어오고 나머지를 LLM 생성. 0.5 기본.
        const ratio = form.extract_ratio ?? 0.5;
        const extractCount = Math.round(form.count * ratio);
        const generateCount = form.count - extractCount;
        r = await hybridAssessment(domainId, {
          extract: {
            subject: form.subject || undefined,
            chapter: form.chapter || undefined,
            count: extractCount,
          },
          generate: {
            subject: form.subject,
            chapter: form.chapter || undefined,
            count: generateCount,
            difficulty: form.difficulty,
          },
        });
      }
      setResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'failed');
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-4 text-gray-900 dark:text-slate-100">Generation Workbench</h1>

      <div className="flex gap-2 mb-4">
        {(['extract', 'generate', 'hybrid', 'figure-reuse'] as Mode[]).map((m) => (
          <Button
            key={m}
            variant={mode === m ? 'primary' : 'secondary'}
            size="sm"
            onClick={() => setMode(m)}
          >
            {m}
          </Button>
        ))}
      </div>

      <div className="grid grid-cols-5 gap-2 mb-4 text-sm">
        <div>
          <label className="block text-xs text-gray-500 dark:text-slate-400">subject</label>
          <input
            value={form.subject}
            onChange={(e) => setForm({ ...form, subject: e.target.value })}
            className="w-full px-2 py-1 border rounded"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 dark:text-slate-400">chapter</label>
          <input
            value={form.chapter}
            onChange={(e) => setForm({ ...form, chapter: e.target.value })}
            className="w-full px-2 py-1 border rounded"
          />
        </div>
        <div>
          <label className="block text-xs text-gray-500 dark:text-slate-400">difficulty</label>
          <select
            value={form.difficulty}
            onChange={(e) => setForm({ ...form, difficulty: e.target.value })}
            className="w-full px-2 py-1 border rounded"
          >
            <option value="easy">easy</option>
            <option value="medium">medium</option>
            <option value="hard">hard</option>
          </select>
        </div>
        <div>
          <label className="block text-xs text-gray-500 dark:text-slate-400">count</label>
          <input
            type="number"
            value={form.count}
            onChange={(e) => setForm({ ...form, count: Number(e.target.value) })}
            min={1}
            max={20}
            className="w-full px-2 py-1 border rounded"
          />
        </div>
        {mode === 'hybrid' && (
          <div>
            <label className="block text-xs text-gray-500 dark:text-slate-400">extract_ratio</label>
            <input
              type="number"
              step="0.1"
              value={form.extract_ratio}
              onChange={(e) =>
                setForm({ ...form, extract_ratio: Number(e.target.value) })
              }
              min={0}
              max={1}
              className="w-full px-2 py-1 border rounded"
            />
          </div>
        )}
      </div>

      <Button onClick={handleRun} disabled={running}>
        {running ? '실행 중...' : `▶ ${mode}`}
      </Button>

      {error && <p className="text-red-600 dark:text-red-400 mt-3">{error}</p>}

      {figureSummary && (
        <div className="mt-4 text-sm">
          {figureSummary.vlm_unavailable && (
            <p className="rounded-lg bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-300 px-3 py-2 mb-2">
              ⚠ VLM(Qwen-VL) 비가동 — 그림 기반 생성을 건너뛰었습니다(degrade). 생성 0건.
            </p>
          )}
          {!figureSummary.vlm_unavailable &&
            figureSummary.generated_count === 0 && (
              <p className="rounded-lg bg-amber-50 dark:bg-amber-900/30 border border-amber-200 dark:border-amber-800 text-amber-800 dark:text-amber-300 px-3 py-2 mb-2">
                ⚠ 재사용 가능한 그림 문항이 없습니다 — 해당 subject에 figure_dependent
                approved 문항이 있어야 합니다.
              </p>
            )}
          <p className="text-gray-500 dark:text-slate-400">
            참조 그림 {figureSummary.references_used}건 · 생성{' '}
            {figureSummary.generated_count}건 · 이미지없음 스킵{' '}
            {figureSummary.skipped_no_image}건 · 검증실패 거부{' '}
            {figureSummary.rejected_invalid}건 · {figureSummary.latency_ms}ms
          </p>
          <p className="text-xs text-gray-400 dark:text-slate-500 mt-0.5">
            생성물은 draft 상태입니다 — Quality Review Queue에서 검수 후 승인하세요.
          </p>
        </div>
      )}

      {result && (
        <div className="mt-6">
          <p className="font-bold mb-2 text-gray-900 dark:text-slate-100">결과 ({result.items.length}건)</p>
          <ul className="space-y-3">
            {result.items.map((it, i) => (
              <li key={i} className="bg-white dark:bg-slate-800 border border-gray-200 dark:border-slate-700 rounded-2xl shadow-card p-3">
                <div className="font-medium mb-1 text-gray-900 dark:text-slate-100">{it.question_text}</div>
                <ul className="text-sm text-gray-700 dark:text-slate-300 mb-1">
                  {it.choices.map((c, ci) => (
                    <li key={ci}>
                      {String.fromCharCode(65 + ci)}. {c}{' '}
                      {c === it.answer && <span className="text-green-600 dark:text-green-400">✓</span>}
                    </li>
                  ))}
                </ul>
                {it.explanation && (
                  <p className="text-xs text-gray-500 dark:text-slate-400">해설: {it.explanation}</p>
                )}
                <p className="text-xs mt-1 text-gray-500 dark:text-slate-400">
                  difficulty: {it.difficulty} · status: {it.quality_status} · score:{' '}
                  {it.quality_score?.toFixed(2) || '-'} · source: {it.source}
                </p>
              </li>
            ))}
          </ul>
        </div>
      )}
    </div>
  );
}
