/**
 * Tenant Admin Dashboard — ADR-016 §3.1.
 * KPI 카드 + citation type 분포 + fallback reason 분포.
 */
'use client';

import { useParams } from 'next/navigation';

export default function DashboardPage() {
  const params = useParams<{ tenantId: string }>();
  return (
    <div className="p-6">
      <h1 className="text-2xl font-bold mb-6">대시보드 — {params.tenantId}</h1>

      <div className="grid grid-cols-4 gap-4 mb-8">
        <KpiCard label="총 문서" value="—" />
        <KpiCard label="총 chunk" value="—" />
        <KpiCard label="오늘 질문" value="—" />
        <KpiCard label="평균 응답" value="—" />
        <KpiCard label="인덱싱 실패" value="—" />
        <KpiCard label="Citation 없음" value="—" />
        <KpiCard label="부정 피드백" value="—" />
        <KpiCard label="fallback 비율" value="—" />
      </div>

      <p className="text-sm text-gray-500">
        실제 데이터 연동은 ADR-017 §10 GET /api/{params.tenantId}/admin/dashboard 구현 필요.
      </p>
    </div>
  );
}

function KpiCard({ label, value }: { label: string; value: string }) {
  return (
    <div className="border border-gray-200 rounded p-4">
      <p className="text-sm text-gray-500">{label}</p>
      <p className="text-2xl font-bold">{value}</p>
    </div>
  );
}
