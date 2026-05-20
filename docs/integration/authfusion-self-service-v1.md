# Self-Service API — RP 측 사용자/관리자 본인 계정 관리

> AuthFusion (IdP) 는 REST API 만 제공. **RP (WiSentinel 등) 측이 자체 portal 페이지**를 만들어
> OIDC Bearer 로 호출. console.aines.kr (admin plane) 트래픽 분리 + RP 자체 도메인/디자인 자유.
>
> 패턴: Okta Self-Service Account API / Auth0 / Entra B2C 와 동일.

---

## 0. 전제

- 사용자가 RP 측 (예 `sse.aines.kr`) 에 이미 OIDC 로 로그인됨 → access token 보유
- RP 측 portal 페이지가 그 access token 으로 `https://api.aines.kr/...` 호출
- **모든 endpoint 는 본인 (authentication.principal = user UUID) 한정** — 다른 사용자 정보 수정 불가능
- 인증 실패 시 `401` (RP 가 refresh + retry), 권한 부족 시 `403`

```
Authorization: Bearer <access_token>
Content-Type: application/json
```

---

## 1. Endpoint 목록 (Base: `https://api.aines.kr`)

| 용도 | Method | Path | 응답 |
|---|---|---|---|
| 본인 프로필 | GET | `/api/v1/me` | `UserResponse` |
| 본인 요약 (sub/username/email) | GET | `/api/v1/me/summary` | `{sub, username, email, userSource}` |
| 본인 application + 권한 | GET | `/api/v1/me/applications` | `UserApplicationSummary[]` |
| 본인 active 세션 | GET | `/api/v1/me/sessions` | `SessionInfo[]` |
| 세션 종료 | DELETE | `/api/v1/me/sessions/{sessionId}` | `204` |
| MFA 상태 | GET | `/api/v1/me/mfa/status` | `MfaStatusResponse` |
| **MFA 활성 시작 (TOTP secret + QR)** | POST | `/api/v1/me/mfa/setup` | `TotpSetupResponse` |
| **MFA 활성 확정 (첫 코드 검증)** | POST | `/api/v1/me/mfa/verify-setup` | `200` |
| **MFA 비활성** | POST | `/api/v1/me/mfa/disable` | `204` |
| **Recovery code 재발급** | POST | `/api/v1/me/mfa/recovery-codes/regenerate` | `string[]` |
| 비밀번호 변경 | POST | `/api/v1/me/change-password` | `204` |

---

## 2. UX 흐름

### 2.1 비밀번호 변경

```http
POST /api/v1/me/change-password
{
  "currentPassword": "...",
  "newPassword": "..."
}
```

검증: 현재 비밀번호 + 새 비밀번호 정책 (이력/만료/길이). 실패 시 `400` + 메시지.

### 2.2 MFA 활성화 (TOTP)

```
[RP 페이지]                          [AuthFusion API]
  사용자: "MFA 활성" 클릭
  ──────────────────────────────────▶ POST /api/v1/me/mfa/setup
                                        { secret, qrCodeUri, recoveryCodes[] }
  ◀──────────────────────────────────
  QR 표시 + recovery codes 표시
  사용자: Google Auth 에 등록 + 첫 코드 입력
  ──────────────────────────────────▶ POST /api/v1/me/mfa/verify-setup
                                        { "code": "123456" }
                                        200 (활성 확정)
  ◀──────────────────────────────────
  "활성화 완료" 표시
```

- `setup` 응답은 *1회용* — 페이지 reload 시 재호출하면 새 secret 발급 (기존 무효)
- `verify-setup` 실패 시 같은 secret 으로 재시도 가능
- recovery codes: 8자 string 8개. 화면 표시 후 사용자 안전 보관 권장

### 2.3 MFA 비활성

```http
POST /api/v1/me/mfa/disable
```

권장: RP 페이지에서 *현재 비밀번호 재입력* 받아 `POST /me/change-password` 같은 패턴으로
한 번 더 본인 증명 후 비활성. (본 endpoint 는 이미 인증된 세션이므로 추가 검증 없음 — RP UX 결정.)

### 2.4 세션 관리

```http
GET /api/v1/me/sessions
# → [{ sessionId, ipAddress, userAgent, lastActivityAt, createdAt, expiresAt }, ...]

DELETE /api/v1/me/sessions/{sessionId}
# → 204
```

자기 sessionId 만 종료 가능 (타인 시도 시 `403`).

---

## 3. 응답 DTO

### `MfaStatusResponse`
```json
{
  "enabled": true,
  "algorithm": "HmacSHA1",
  "digits": 6,
  "period": 30,
  "verifiedAt": "2026-05-20T15:49:07",
  "recoveryCodesRemaining": 7
}
```

### `TotpSetupResponse`
```json
{
  "secret": "JBSWY3DPEHPK3PXP",
  "qrCodeUri": "otpauth://totp/AuthFusion:juchul?secret=JBSWY3DPEHPK3PXP&issuer=AuthFusion&algorithm=SHA1&digits=6&period=30",
  "recoveryCodes": ["A1B2-C3D4", "..."]
}
```

`qrCodeUri` 를 `qrcode.js` 같은 라이브러리로 PNG 렌더링 → 사용자가 Google Authenticator / MS Authenticator 로 스캔.

### `SessionInfo`
```json
{
  "sessionId": "uuid",
  "ipAddress": "1.2.3.4",
  "userAgent": "Mozilla/...",
  "createdAt": "2026-05-20T10:00:00",
  "lastActivityAt": "2026-05-20T15:30:00",
  "expiresAt": "2026-05-20T18:00:00"
}
```

---

## 4. 권한 / 보안

- 모든 endpoint = `isAuthenticated()` + 본인 principal 한정 (IDOR 불가능)
- access token 만료 시 `401` → RP 가 refresh token 으로 갱신 후 재시도
- 본인 sessionId 가 아닌 시도 → `403 Cannot revoke another user's session`
- 비밀번호 변경 실패 (현재 비번 틀림 / 정책 위반) → `400`
- **CSRF**: bearer token + cors+SameSite 패턴이므로 RP 가 직접 호출 시 별도 CSRF 토큰 불필요

---

## 5. RP 구현 권장 페이지

```
sse.aines.kr/account                  → 본인 정보 (GET /me + /me/applications)
sse.aines.kr/account/security          → 비밀번호 변경 + MFA 토글
sse.aines.kr/account/sessions          → 세션 목록 + 종료
```

페이지 디자인 / i18n / 추가 위젯은 RP 자유. AuthFusion API 호출만 표준화.

---

## 6. WiSentinel 측 다음 단계

1. `sse.aines.kr/account` route + UI 구현
2. 위 11 endpoint 호출 (axios + Bearer)
3. token 만료 401 → SDK 의 refresh interceptor (이미 SDK 있다면 그대로)
4. 통합 테스트: 비밀번호 변경 / MFA enroll → verify / disable / recovery / 세션 종료
5. 사용자 안내 (FAQ) — recovery codes 보관 권장

질문: AuthFusion 운영팀

---

## 부록 — 실제 운영 응답 shape (2026-05-20 E2E 검증 ground truth)

spec 본문과 일부 field 이름이 다름. 운영 진실은 아래 (DomainRAG frontend는 이 shape로 type 정의).

### MfaStatusResponse (실)
```json
{
  "userId": "0d0a8e87-...",
  "totpEnabled": true,
  "totpVerified": true,
  "recoveryCodesRemaining": 10,
  "totpEnabledAt": "2026-05-19T12:08:54.958861"
}
```
spec 대비: `enabled`→`totpEnabled`/`totpVerified` 분리. `algorithm`/`digits`/`period` 없음. `verifiedAt`→`totpEnabledAt`.

### UserApplicationSummary (실)
```json
{
  "clientUuid": "f38981be-...",
  "clientId": "6cb9d56c-...",
  "clientName": "DomainRAG Ops",
  "enabled": true,
  "mfaRequired": true,
  "roles": ["domainrag-ops-user"],
  "grantedAt": "2026-05-18T22:07:14.458895"
}
```
spec 대비: `applicationId`→`clientUuid`(또는 `clientId`). `displayName`→`clientName`. `enabled`/`mfaRequired`/`grantedAt` 추가.

### SessionInfo (실)
```json
{
  "sessionId": "b051e0e0-...",
  "userId": "0d0a8e87-...",
  "username": "juchul",
  "ipAddress": "192.168.0.1",
  "userAgent": "Mozilla/5.0 ...",
  "status": "ACTIVE",
  "createdAt": "2026-05-20T12:31:11.224612854Z",
  "lastAccessedAt": "2026-05-20T12:31:11.224612854Z",
  "expiresAt": "2026-05-20T13:31:11.224612854Z"
}
```
spec 대비: `lastActivityAt`→`lastAccessedAt`. `userId`/`username`/`status` 추가.

AuthFusion 운영팀에 spec 본문 갱신 요청 권장.
