'use client';

import Card, { CardHeader } from '@/components/ui/Card';
import Badge from '@/components/ui/Badge';
import Button from '@/components/ui/Button';

export default function SessionsPage() {
  return (
    <div className="space-y-6">
      <Card>
        <CardHeader
          title="활성 세션"
          description="이 계정으로 로그인된 모든 device · 브라우저 목록."
          action={<Badge tone="warn">준비 중</Badge>}
        />
        <div className="text-sm text-gray-600 space-y-3">
          <p>
            세션 관리는 AuthFusion REST API gap 보완 후 활성화됩니다
            (예정: <code className="text-xs bg-gray-100 px-1 rounded">/api/v1/users/me/sessions</code>).
          </p>
          <div className="bg-gray-50 border border-gray-200 rounded-lg p-4">
            <p className="text-xs text-gray-500 mb-2">활성화되면 다음 정보를 표시합니다:</p>
            <ul className="text-xs text-gray-700 list-disc pl-4 space-y-1">
              <li>device · 브라우저 종류 + 최초 로그인 시각</li>
              <li>최근 활동 시각, IP, 위치(국가/도시)</li>
              <li>각 세션 개별 종료 + "모든 세션 종료" 버튼</li>
            </ul>
          </div>
        </div>
      </Card>

      <Card>
        <CardHeader
          title="모든 device 로그아웃"
          description="계정 도용이 의심되면 모든 device에서 강제 로그아웃."
          action={<Badge tone="warn">준비 중</Badge>}
        />
        <Button variant="danger" disabled>
          모든 세션 종료
        </Button>
        <p className="text-[11px] text-gray-500 mt-3">
          AuthFusion <code className="text-xs bg-gray-100 px-1 rounded">/api/v1/auth/logout-all</code> endpoint 준비 후 활성화.
        </p>
      </Card>
    </div>
  );
}
