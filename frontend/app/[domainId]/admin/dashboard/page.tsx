/**
 * Tenant Admin Dashboard — ADR-016 §3.1 + ADR-017 §10.
 *
 * KPI 카드 + citation type 분포 + fallback reason 분포 + routing distribution.
 * GET /api/{tid}/admin/dashboard 결선.
 */
'use client';

import { useParams } from 'next/navigation';
import useSWR from 'swr';
import Card from '@/components/ui/Card';
import { getAssessmentAnalytics, getDashboard, swrFetcher } from '@/lib/api';
import type {
  AssessmentAnalytics,
  AssessmentQualityStatus,
  DashboardSnapshot,
  SupportType,
} from '@/lib/types';

const ASSESSMENT_STATUS_LABEL: Record<AssessmentQualityStatus, string> = {
  approved: '승인',
  reviewed: '검수완료',
  draft: '초안',
  retired: '비활성',
};

const ASSESSMENT_STATUS_COLOR: Record<AssessmentQualityStatus, string> = {
  approved: 'bg-green-500',
  reviewed: 'bg-blue-500',
  draft: 'bg-amber-500',
  retired: 'bg-gray-400',
};

const SUPPORT_TYPE_LABEL: Record<SupportType, string> = {
  direct: '직접',
  synthesis: '종합',
  inference: '추론',
  conflict: '충돌',
};

const SUPPORT_TYPE_COLOR: Record<SupportType, string> = {
  direct: 'bg-citation-direct',
  synthesis: 'bg-citation-synthesis',
  inference: 'bg-citation-inference',
  conflict: 'bg-citation-conflict',
};

export default function DashboardPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;

  const { data, error, isLoading } = useSWR<DashboardSnapshot>(
    domainId ? `dashboard:${domainId}` : null,
    () => getDashboard(domainId),
    { refreshInterval: 30000 },
  );

  // assessment 모듈 활성 도메인에서만 시험 문항 섹션 노출 (ADR-014 §8, nav 게이팅과 정합).
  const { data: modulesData } = useSWR<{ modules: string[] }>(
    domainId ? `/api/${domainId}/admin/modules` : null,
    swrFetcher,
  );
  const assessmentActive = (modulesData?.modules ?? []).includes('assessment');

  const { data: assessment } = useSWR<AssessmentAnalytics>(
    assessmentActive ? `assessment-analytics:${domainId}` : null,
    () => getAssessmentAnalytics(domainId),
    { refreshInterval: 60000 },
  );

  if (isLoading) return <div className="p-6 text-gray-700 dark:text-slate-300">로딩 중...</div>;
  if (error) {
    return (
      <div className="p-6 text-red-600 dark:text-red-400">
        대시보드 로드 실패: {error.message}
      </div>
    );
  }
  if (!data) return null;

  const totalCitations = Object.values(data.citation_type_distribution).reduce(
    (a, b) => a + b,
    0,
  );

  return (
    <div className="p-8 max-w-7xl mx-auto font-sans">
      <div className="mb-8 flex items-end justify-between">
        <div>
          <h1 className="text-2xl font-semibold text-gray-900 dark:text-slate-100 tracking-tight">대시보드</h1>
          <p className="text-sm text-gray-500 dark:text-slate-400 mt-1">
            <span className="font-medium text-gray-700 dark:text-slate-200">{domainId}</span> tenant · 오늘 KST 기준
          </p>
        </div>
        <span className="text-[11px] text-gray-400 dark:text-slate-500">
          30초마다 자동 갱신
        </span>
      </div>

      <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-8">
        <KpiCard label="총 문서" value={data.total_documents.toLocaleString()} />
        <KpiCard label="총 chunk" value={data.total_chunks.toLocaleString()} />
        <KpiCard label="오늘 업로드" value={data.uploaded_today.toLocaleString()} />
        <KpiCard label="오늘 인덱싱 실패" value={data.indexing_failed_today.toLocaleString()} accent={data.indexing_failed_today > 0 ? 'warn' : undefined} />
        <KpiCard label="오늘 질문" value={data.questions_today.toLocaleString()} />
        <KpiCard
          label="평균 응답"
          value={`${(data.avg_latency_ms / 1000).toFixed(2)}s`}
        />
        <KpiCard label="Citation 없음" value={data.answers_without_citation.toLocaleString()} />
        <KpiCard
          label="부정 피드백"
          value={`${(data.negative_feedback_rate * 100).toFixed(1)}%`}
          accent={data.negative_feedback_rate > 0.1 ? 'warn' : undefined}
        />
      </div>

      {assessmentActive && assessment && (
        <div className="mb-8">
          <h2 className="text-sm font-semibold text-gray-900 dark:text-slate-100 mb-3">
            시험 문항 (현재 누적)
          </h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-3 mb-6">
            <KpiCard label="총 문항" value={assessment.total_items.toLocaleString()} />
            <KpiCard
              label="승인"
              value={(assessment.by_quality_status.approved ?? 0).toLocaleString()}
            />
            <KpiCard
              label="검수 대기"
              value={(
                (assessment.by_quality_status.draft ?? 0) +
                (assessment.by_quality_status.reviewed ?? 0)
              ).toLocaleString()}
              accent={
                (assessment.by_quality_status.draft ?? 0) +
                  (assessment.by_quality_status.reviewed ?? 0) >
                0
                  ? 'warn'
                  : undefined
              }
            />
            <KpiCard
              label="그림 의존"
              value={assessment.figure_dependent.toLocaleString()}
            />
          </div>
          <div className="grid grid-cols-2 gap-6">
            <Section title="상태 분포">
              {assessment.total_items === 0 && (
                <p className="text-sm text-gray-400 dark:text-slate-500">아직 문항 없음.</p>
              )}
              {(['approved', 'reviewed', 'draft', 'retired'] as AssessmentQualityStatus[]).map(
                (s) => {
                  const v = assessment.by_quality_status[s] ?? 0;
                  const pct = assessment.total_items
                    ? (v / assessment.total_items) * 100
                    : 0;
                  return (
                    <BarRow
                      key={s}
                      label={ASSESSMENT_STATUS_LABEL[s]}
                      value={v}
                      percent={pct}
                      colorClass={ASSESSMENT_STATUS_COLOR[s]}
                    />
                  );
                },
              )}
            </Section>

            <Section title="과목 분포 (상위 8)">
              {Object.keys(assessment.by_subject).length === 0 && (
                <p className="text-sm text-gray-400 dark:text-slate-500">아직 과목 없음.</p>
              )}
              {Object.entries(assessment.by_subject)
                .sort(([, a], [, b]) => b - a)
                .slice(0, 8)
                .map(([subject, count]) => (
                  <BarRow
                    key={subject}
                    label={subject}
                    value={count}
                    percent={
                      assessment.total_items
                        ? (count / assessment.total_items) * 100
                        : 0
                    }
                    colorClass="bg-indigo-500"
                  />
                ))}
            </Section>
          </div>
        </div>
      )}

      <div className="grid grid-cols-2 gap-6">
        <Section title="Citation Type 분포 (오늘)">
          {totalCitations === 0 && (
            <p className="text-sm text-gray-400 dark:text-slate-500">아직 데이터 없음.</p>
          )}
          {(['direct', 'synthesis', 'inference', 'conflict'] as SupportType[]).map(
            (t) => {
              const v = data.citation_type_distribution[t] || 0;
              const pct = totalCitations ? (v / totalCitations) * 100 : 0;
              return (
                <BarRow
                  key={t}
                  label={`${SUPPORT_TYPE_LABEL[t]} (${t})`}
                  value={v}
                  percent={pct}
                  colorClass={SUPPORT_TYPE_COLOR[t]}
                />
              );
            },
          )}
        </Section>

        <Section title="Fallback Reason 분포">
          {Object.keys(data.fallback_distribution).length === 0 && (
            <p className="text-sm text-gray-400 dark:text-slate-500">아직 fallback 없음.</p>
          )}
          {Object.entries(data.fallback_distribution)
            .sort(([, a], [, b]) => b - a)
            .map(([reason, count]) => (
              <BarRow
                key={reason}
                label={reason}
                value={count}
                percent={0}
                colorClass="bg-yellow-500"
              />
            ))}
        </Section>

        <Section title="Routing Decision 분포">
          {Object.keys(data.routing_distribution).length === 0 && (
            <p className="text-sm text-gray-400 dark:text-slate-500">아직 라우팅 결정 없음.</p>
          )}
          {Object.entries(data.routing_distribution)
            .sort(([, a], [, b]) => b - a)
            .map(([key, count]) => (
              <BarRow
                key={key}
                label={key}
                value={count}
                percent={0}
                colorClass="bg-blue-500"
              />
            ))}
        </Section>

      </div>
    </div>
  );
}

function KpiCard({
  label,
  value,
  accent,
}: {
  label: string;
  value: string;
  accent?: 'warn';
}) {
  return (
    <Card className="rounded-xl px-4 py-3 hover:shadow-sm transition-shadow" padded={false}>
      <p className="text-xs text-gray-500 dark:text-slate-400 font-medium">{label}</p>
      <p
        className={`text-2xl font-semibold mt-1 ${
          accent === 'warn' ? 'text-amber-600 dark:text-amber-400' : 'text-gray-900 dark:text-slate-100'
        }`}
      >
        {value}
      </p>
    </Card>
  );
}

function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <Card className="rounded-xl">
      <h2 className="text-sm font-semibold text-gray-900 dark:text-slate-100 mb-4">{title}</h2>
      <div className="space-y-3">{children}</div>
    </Card>
  );
}

function BarRow({
  label,
  value,
  percent,
  colorClass,
}: {
  label: string;
  value: number;
  percent: number;
  colorClass: string;
}) {
  return (
    <div>
      <div className="flex justify-between text-sm">
        <span>{label}</span>
        <span className="text-gray-600">
          {value.toLocaleString()}
          {percent > 0 && ` (${percent.toFixed(1)}%)`}
        </span>
      </div>
      <div className="h-2 bg-gray-100 rounded overflow-hidden mt-1">
        <div
          className={`h-full ${colorClass}`}
          style={{ width: `${Math.min(percent || (value > 0 ? 100 : 0), 100)}%` }}
        />
      </div>
    </div>
  );
}
