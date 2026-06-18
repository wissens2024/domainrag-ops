/**
 * 기출 PDF Import — /{domainId}/admin/assessment/import (ADR-025 §2).
 *
 * PDF 업로드 → 서버가 텍스트 파싱 + 그림 crop·자산 저장 → draft item 일괄 생성.
 * 결과는 사람 검수 전제(기본 draft) — Review Queue에서 확인·승인.
 */
'use client';

import { useParams } from 'next/navigation';
import { useState } from 'react';
import Badge from '@/components/ui/Badge';
import Button from '@/components/ui/Button';
import Card, { CardHeader } from '@/components/ui/Card';
import Input from '@/components/ui/Input';
import { importAssessmentPdf } from '@/lib/api';
import type { AssessmentImportResult, AssessmentQualityStatus } from '@/lib/types';

export default function AssessmentImportPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;

  const [file, setFile] = useState<File | null>(null);
  const [prefix, setPrefix] = useState('');
  const [answerPage, setAnswerPage] = useState('');
  const [quality, setQuality] = useState<AssessmentQualityStatus>('draft');
  const [tags, setTags] = useState('');
  const [running, setRunning] = useState(false);
  const [result, setResult] = useState<AssessmentImportResult | null>(null);
  const [error, setError] = useState<string | null>(null);

  const canRun = !!file && prefix.trim().length > 0 && !running;

  const handleRun = async () => {
    if (!file) return;
    setRunning(true);
    setError(null);
    setResult(null);
    try {
      const r = await importAssessmentPdf(domainId, file, {
        item_id_prefix: prefix.trim(),
        answer_page_index: answerPage.trim() ? Number(answerPage) : undefined,
        default_quality_status: quality,
        tags: tags.trim() || undefined,
      });
      setResult(r);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'import 실패');
    } finally {
      setRunning(false);
    }
  };

  return (
    <div className="p-6 max-w-4xl">
      <h1 className="text-2xl font-bold mb-1 text-gray-900 dark:text-slate-100">
        기출 PDF 가져오기
      </h1>
      <p className="text-sm text-gray-500 dark:text-slate-400 mb-5">
        PDF를 올리면 문항·정답을 파싱하고 그림을 추출해 문제은행에 draft로 등록합니다.
        등록 결과는 검수 큐에서 확인·승인하세요.
      </p>

      <Card>
        <CardHeader
          title="업로드"
          description="문항 + 말미 정답표 형식의 기출 PDF (예: 정보처리기사 필기)"
        />
        <div className="space-y-4">
          <div>
            <label className="block text-xs font-medium text-gray-700 dark:text-slate-300 mb-1.5">
              PDF 파일
            </label>
            <input
              type="file"
              accept="application/pdf,.pdf"
              onChange={(e) => setFile(e.target.files?.[0] ?? null)}
              className="block w-full text-sm text-gray-700 dark:text-slate-300
                file:mr-3 file:py-2 file:px-4 file:rounded-lg file:border-0
                file:text-sm file:font-medium file:bg-gray-900 file:text-white
                hover:file:bg-gray-700 dark:file:bg-brand-600 dark:hover:file:bg-brand-500"
            />
          </div>

          <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
            <Input
              label="item_id 접두 (item_id_prefix)"
              placeholder="gisa-2022-2-w-"
              hint="생성 item_id 접두. 예: gisa-2022-2-w- → gisa-2022-2-w-037"
              value={prefix}
              onChange={(e) => setPrefix(e.target.value)}
            />
            <Input
              label="정답표 페이지 (0-based, 선택)"
              type="number"
              placeholder="미지정 시 마지막 페이지"
              hint="정답표가 마지막 페이지면 비워두세요."
              value={answerPage}
              onChange={(e) => setAnswerPage(e.target.value)}
            />
            <div>
              <label className="block text-xs font-medium text-gray-700 dark:text-slate-300 mb-1.5">
                기본 상태 (default_quality_status)
              </label>
              <select
                value={quality}
                onChange={(e) => setQuality(e.target.value as AssessmentQualityStatus)}
                className="w-full px-3 py-2 text-sm bg-white border border-gray-300 rounded-lg
                  dark:bg-slate-900 dark:border-slate-600 dark:text-slate-100
                  focus:outline-none focus:border-gray-900 focus:ring-1 focus:ring-gray-900
                  dark:focus:border-brand-500 dark:focus:ring-brand-500"
              >
                <option value="draft">draft (검수 대기)</option>
                <option value="reviewed">reviewed (검토됨)</option>
                <option value="approved">approved (출제 가능)</option>
              </select>
            </div>
            <Input
              label="태그 (쉼표 구분, 선택)"
              placeholder="2022,2회,필기"
              value={tags}
              onChange={(e) => setTags(e.target.value)}
            />
          </div>

          <div className="flex items-center gap-3">
            <Button onClick={handleRun} disabled={!canRun}>
              {running ? '가져오는 중...' : '가져오기'}
            </Button>
            {error && <span className="text-sm text-red-600 dark:text-red-400">{error}</span>}
          </div>
        </div>
      </Card>

      {result && (
        <Card className="mt-5">
          <CardHeader
            title="가져오기 결과"
            description={`도메인 ${result.domain_id}`}
            action={
              <div className="flex gap-2">
                <Badge tone="success">생성 {result.created}</Badge>
                <Badge tone="info">그림 {result.figures_stored}</Badge>
              </div>
            }
          />
          <p className="text-xs text-gray-500 dark:text-slate-400 mb-3">
            파싱 {result.parsed_count}문항 · 정답키 {result.answer_key_count}개
          </p>
          <div className="overflow-x-auto">
            <table className="w-full text-sm">
              <thead>
                <tr className="text-left text-xs text-gray-500 dark:text-slate-400 border-b border-gray-200 dark:border-slate-700">
                  <th className="py-2 pr-3">번호</th>
                  <th className="py-2 pr-3">item_id</th>
                  <th className="py-2 pr-3">과목</th>
                  <th className="py-2 pr-3">상태</th>
                  <th className="py-2 pr-3">표식</th>
                </tr>
              </thead>
              <tbody>
                {result.items.map((it) => (
                  <tr
                    key={it.item_id}
                    className="border-b border-gray-100 dark:border-slate-800 text-gray-800 dark:text-slate-200"
                  >
                    <td className="py-2 pr-3">{it.number}</td>
                    <td className="py-2 pr-3 font-mono text-xs">{it.item_id}</td>
                    <td className="py-2 pr-3">{it.subject ?? '-'}</td>
                    <td className="py-2 pr-3">
                      <Badge tone="neutral">{it.quality_status}</Badge>
                    </td>
                    <td className="py-2 pr-3">
                      <div className="flex flex-wrap gap-1">
                        {it.figure_dependent && (
                          <Badge tone="info">그림 {it.asset_count}</Badge>
                        )}
                        {!it.has_answer && <Badge tone="danger">정답없음</Badge>}
                        {it.flags.includes('code_or_sql') && (
                          <Badge tone="warn">코드/SQL</Badge>
                        )}
                        {it.flags
                          .filter((f) => f.startsWith('choices='))
                          .map((f) => (
                            <Badge key={f} tone="warn">
                              {f}
                            </Badge>
                          ))}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Card>
      )}
    </div>
  );
}
