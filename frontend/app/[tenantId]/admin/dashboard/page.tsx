/**
 * Tenant Admin Dashboard — ADR-016 §3.1 + ADR-017 §10.
 *
 * KPI 카드 + citation type 분포 + fallback reason 분포 + routing distribution.
 * GET /api/{tid}/admin/dashboard 결선.
 */
'use client';

import { useParams } from 'next/navigation';
import useSWR from 'swr';
import { getDashboard } from '@/lib/api';
import type { DashboardSnapshot, SupportType } from '@/lib/types';

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
  const params = useParams<{ tenantId: string }>();
  const tenantId = params.tenantId;

  const { data, error, isLoading } = useSWR<DashboardSnapshot>(
    tenantId ? `dashboard:${tenantId}` : null,
    () => getDashboard(tenantId),
    { refreshInterval: 30000 },
  );

  if (isLoading) return <div className="p-6">로딩 중...</div>;
  if (error) {
    return (
      <div className="p-6 text-red-600">
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
          <h1 className="text-2xl font-semibold text-gray-900 tracking-tight">대시보드</h1>
          <p className="text-sm text-gray-500 mt-1">
            <span className="font-medium text-gray-700">{tenantId}</span> tenant · 오늘 KST 기준
          </p>
        </div>
        <span className="text-[11px] text-gray-400">
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

      <div className="grid grid-cols-2 gap-6">
        <Section title="Citation Type 분포 (오늘)">
          {totalCitations === 0 && (
            <p className="text-sm text-gray-400">아직 데이터 없음.</p>
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
            <p className="text-sm text-gray-400">아직 fallback 없음.</p>
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
            <p className="text-sm text-gray-400">아직 라우팅 결정 없음.</p>
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
    <div className="bg-white border border-gray-200 rounded-xl px-4 py-3 hover:shadow-sm transition-shadow">
      <p className="text-xs text-gray-500 font-medium">{label}</p>
      <p
        className={`text-2xl font-semibold mt-1 ${
          accent === 'warn' ? 'text-amber-600' : 'text-gray-900'
        }`}
      >
        {value}
      </p>
    </div>
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
    <div className="bg-white border border-gray-200 rounded-xl p-5">
      <h2 className="text-sm font-semibold text-gray-900 mb-4">{title}</h2>
      <div className="space-y-3">{children}</div>
    </div>
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
