'use client';

import { useState } from 'react';
import { QRCodeSVG } from 'qrcode.react';
import useSWR, { mutate } from 'swr';
import Card, { CardHeader } from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import Input from '@/components/ui/Input';
import Badge from '@/components/ui/Badge';
import { account, ApiError } from '@/lib/api';
import type { TotpSetupResponse } from '@/lib/api';

const MFA_KEY = 'account.mfa.status';

function formatApiError(e: unknown): string {
  if (e instanceof ApiError) {
    const d = e.detail as Record<string, unknown> | undefined;
    const msg = (d?.message ?? d?.error) as string | undefined;
    return msg ?? `HTTP ${e.status}`;
  }
  return e instanceof Error ? e.message : '실패';
}

export default function SecurityPage() {
  const { data: mfa } = useSWR(MFA_KEY, () => account.getMfaStatus());

  return (
    <div className="space-y-6">
      <PasswordCard />
      <MfaCard mfaEnabled={mfa?.enabled ?? false} mfaRemaining={mfa?.recoveryCodesRemaining} />
    </div>
  );
}

function PasswordCard() {
  const [current, setCurrent] = useState('');
  const [next1, setNext1] = useState('');
  const [next2, setNext2] = useState('');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);

  const submit = async (e: React.FormEvent) => {
    e.preventDefault();
    setMsg(null);
    if (next1.length < 8) {
      setMsg({ kind: 'err', text: '새 비밀번호는 8자 이상이어야 합니다.' });
      return;
    }
    if (next1 !== next2) {
      setMsg({ kind: 'err', text: '새 비밀번호가 일치하지 않습니다.' });
      return;
    }
    setLoading(true);
    try {
      await account.changePassword(current, next1);
      setMsg({ kind: 'ok', text: '비밀번호가 변경되었습니다.' });
      setCurrent('');
      setNext1('');
      setNext2('');
    } catch (e) {
      setMsg({ kind: 'err', text: formatApiError(e) });
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card>
      <CardHeader
        title="비밀번호"
        description="현재 비밀번호 확인 후 새 비밀번호로 변경합니다. 다른 device의 세션은 유지됩니다 — 모두 끊으려면 세션 탭."
      />
      <form onSubmit={submit} className="space-y-4 max-w-md">
        <Input
          type="password"
          name="current_password"
          label="현재 비밀번호"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
          required
          autoComplete="current-password"
        />
        <Input
          type="password"
          name="new_password"
          label="새 비밀번호"
          value={next1}
          onChange={(e) => setNext1(e.target.value)}
          hint="8자 이상 — 영문·숫자·기호 조합 권장"
          required
          autoComplete="new-password"
        />
        <Input
          type="password"
          name="new_password_confirm"
          label="새 비밀번호 확인"
          value={next2}
          onChange={(e) => setNext2(e.target.value)}
          required
          autoComplete="new-password"
        />
        {msg && (
          <p
            className={`text-xs ${
              msg.kind === 'ok' ? 'text-green-700' : 'text-red-600'
            }`}
          >
            {msg.text}
          </p>
        )}
        <Button
          type="submit"
          loading={loading}
          disabled={!current || !next1 || !next2}
        >
          비밀번호 변경
        </Button>
      </form>
    </Card>
  );
}

function MfaCard({
  mfaEnabled,
  mfaRemaining,
}: {
  mfaEnabled: boolean;
  mfaRemaining: number | undefined;
}) {
  // 활성: 비활성·recovery 재발급
  // 비활성: setup → verify → 활성
  const [stage, setStage] = useState<'idle' | 'setup' | 'verify'>('idle');
  const [setupRes, setSetupRes] = useState<TotpSetupResponse | null>(null);
  const [code, setCode] = useState('');
  const [loading, setLoading] = useState(false);
  const [msg, setMsg] = useState<{ kind: 'ok' | 'err'; text: string } | null>(null);
  const [recovery, setRecovery] = useState<string[] | null>(null);

  const startSetup = async () => {
    setMsg(null);
    setLoading(true);
    try {
      const res = await account.setupMfa();
      setSetupRes(res);
      setStage('verify');
    } catch (e) {
      setMsg({ kind: 'err', text: formatApiError(e) });
    } finally {
      setLoading(false);
    }
  };

  const verifyCode = async (e: React.FormEvent) => {
    e.preventDefault();
    setMsg(null);
    setLoading(true);
    try {
      await account.verifyMfaSetup(code.trim());
      setMsg({ kind: 'ok', text: 'MFA가 활성화되었습니다.' });
      setStage('idle');
      setSetupRes(null);
      setCode('');
      void mutate(MFA_KEY);
    } catch (e) {
      setMsg({ kind: 'err', text: formatApiError(e) });
    } finally {
      setLoading(false);
    }
  };

  const disable = async () => {
    if (!confirm('MFA를 비활성화하시겠습니까? 보안이 약화됩니다.')) return;
    setLoading(true);
    setMsg(null);
    try {
      await account.disableMfa();
      setMsg({ kind: 'ok', text: 'MFA가 비활성화되었습니다.' });
      void mutate(MFA_KEY);
    } catch (e) {
      setMsg({ kind: 'err', text: formatApiError(e) });
    } finally {
      setLoading(false);
    }
  };

  const regenerate = async () => {
    if (!confirm('새 recovery code를 발급합니다. 기존 코드는 모두 무효화됩니다. 계속할까요?'))
      return;
    setLoading(true);
    setMsg(null);
    try {
      const codes = await account.regenerateRecoveryCodes();
      setRecovery(codes);
      void mutate(MFA_KEY);
    } catch (e) {
      setMsg({ kind: 'err', text: formatApiError(e) });
    } finally {
      setLoading(false);
    }
  };

  return (
    <Card>
      <CardHeader
        title="2단계 인증 (MFA)"
        description="TOTP 인증기 앱 (Google Authenticator, Authy 등) 기반 추가 인증."
        action={
          mfaEnabled ? (
            <Badge tone="success">활성</Badge>
          ) : (
            <Badge tone="neutral">비활성</Badge>
          )
        }
      />

      {msg && (
        <p
          className={`text-xs mb-4 ${
            msg.kind === 'ok' ? 'text-green-700' : 'text-red-600'
          }`}
        >
          {msg.text}
        </p>
      )}

      {/* 활성 상태 */}
      {mfaEnabled && stage === 'idle' && (
        <div className="space-y-3">
          {typeof mfaRemaining === 'number' && (
            <p className="text-xs text-gray-500">
              남은 recovery code: <span className="font-medium text-gray-900">{mfaRemaining}개</span>
            </p>
          )}
          <div className="flex gap-2">
            <Button variant="secondary" onClick={regenerate} loading={loading}>
              Recovery code 재발급
            </Button>
            <Button variant="danger" onClick={disable} loading={loading}>
              MFA 비활성화
            </Button>
          </div>
          {recovery && (
            <RecoveryCodesBox codes={recovery} onClose={() => setRecovery(null)} />
          )}
        </div>
      )}

      {/* 비활성 + setup 시작 */}
      {!mfaEnabled && stage === 'idle' && (
        <div>
          <p className="text-sm text-gray-600 mb-4">
            인증기 앱(Google Authenticator 등)을 사용해 로그인 시 추가 코드 입력을
            요구합니다.
          </p>
          <Button onClick={startSetup} loading={loading}>
            MFA 활성화 시작
          </Button>
        </div>
      )}

      {/* setup 진행 — QR + 코드 입력 */}
      {stage === 'verify' && setupRes && (
        <div className="space-y-5">
          <div className="bg-amber-50 border border-amber-200 rounded-lg p-3 text-xs text-amber-800">
            <p className="font-medium mb-1">⚠ 화면을 떠나기 전에:</p>
            <ol className="list-decimal pl-4 space-y-0.5">
              <li>아래 QR을 인증기 앱으로 스캔</li>
              <li>recovery code를 안전한 곳에 보관 (다시 표시 안 됨)</li>
              <li>인증기에 표시된 6자리 코드를 입력해 활성 확정</li>
            </ol>
          </div>

          <div className="flex gap-6 items-start">
            <div className="bg-white border border-gray-200 rounded-xl p-3">
              <QRCodeSVG value={setupRes.qrCodeUri} size={160} />
            </div>
            <div className="flex-1 space-y-3">
              <div>
                <p className="text-[11px] text-gray-500 font-medium uppercase tracking-wide mb-1">
                  Secret (수동 입력용)
                </p>
                <code className="block text-xs font-mono bg-gray-100 px-2 py-1.5 rounded break-all">
                  {setupRes.secret}
                </code>
              </div>
              <RecoveryCodesBox codes={setupRes.recoveryCodes} hideClose />
            </div>
          </div>

          <form onSubmit={verifyCode} className="flex gap-2 items-end max-w-xs">
            <Input
              label="인증기 6자리 코드"
              value={code}
              onChange={(e) => setCode(e.target.value.replace(/\D/g, '').slice(0, 6))}
              inputMode="numeric"
              maxLength={6}
              required
            />
            <Button type="submit" loading={loading} disabled={code.length !== 6}>
              확인
            </Button>
          </form>
        </div>
      )}
    </Card>
  );
}

function RecoveryCodesBox({
  codes,
  onClose,
  hideClose,
}: {
  codes: string[];
  onClose?: () => void;
  hideClose?: boolean;
}) {
  const copyAll = () => {
    void navigator.clipboard?.writeText(codes.join('\n'));
  };
  return (
    <div className="bg-gray-50 border border-gray-200 rounded-lg p-3">
      <div className="flex items-center justify-between mb-2">
        <p className="text-[11px] text-gray-500 font-medium uppercase tracking-wide">
          Recovery codes
        </p>
        <div className="flex gap-2">
          <button
            onClick={copyAll}
            className="text-[11px] text-gray-600 hover:text-gray-900"
          >
            전체 복사
          </button>
          {!hideClose && onClose && (
            <button
              onClick={onClose}
              className="text-[11px] text-gray-400 hover:text-gray-900"
            >
              ✕
            </button>
          )}
        </div>
      </div>
      <div className="grid grid-cols-2 gap-1 font-mono text-xs">
        {codes.map((c, i) => (
          <code key={i} className="bg-white border border-gray-200 px-2 py-1 rounded">
            {c}
          </code>
        ))}
      </div>
      <p className="text-[10px] text-gray-500 mt-2">
        각 코드는 1회만 사용. 안전한 곳에 보관하세요.
      </p>
    </div>
  );
}
