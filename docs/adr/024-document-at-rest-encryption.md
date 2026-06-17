# ADR-024: Document Originals At-Rest Encryption — MinIO SSE-KMS (per-tenant key)

## Status
**Accepted** (2026-06-03)

> ADR-008/019는 원본 문서를 MinIO에 **prefix-per-tenant**로 적재한다고 정했으나, *at-rest 암호화*는 어느 ADR도 규정하지 않았다(ADR-019는 LoRA·secret만, ADR-020은 PII 탐지·audit만). 그 결과 `MinIOStorage.put_object()`가 `sse` 없이 호출되어 **원본이 평문으로 저장**되고 있었다. 본 ADR이 문서 원본 at-rest 암호화를 단일 진실 소스로 정의한다.

---

## Context

### 사실 (현 상태)

- `backend/app/services/document_storage.py`의 `MinIOStorage.save()`는 `client.put_object(...)`를 **`sse` 인자 없이** 호출한다 → MinIO 서버가 자동 암호화를 강제하지 않는 한 평문 저장.
- 운영 MinIO 컨테이너에 KMS/KES가 구성돼 있지 않다(`mc admin kms key list` → *"KMS is not configured"*). 따라서 server-default 암호화도 없다.
- `configs/security.yaml`에 `encryption: {enabled: true, algorithm: AES256}` 블록이 있으나, 이 파일은 `provider: mock`·`HS256`·LDAP가 박힌 **초기 스캐폴드 잔재**로 ADR-018(OIDC RS256)과 충돌하며 어떤 코드 경로에도 배선돼 있지 않다(dead config). at-rest 암호화의 진실 소스가 아니다.
- 암호화가 실제 적용되는 곳은 **LoRA 어댑터(KeyHub envelope, aes-256-gcm)**·**service account secret(LocalSecretStore Fernet)** 뿐이다(ADR-019). 문서 원본은 그 대상이 아니었다.

### 위협 모델 (폐쇄망 전제에서도 유효한 위험)

폐쇄망이라 인터넷 노출 위협은 작지만, 그것이 평문 저장을 정당화하지 않는다.

- 원본에는 **시험문제·정책 문서·PII**(ADR-020)가 포함된다.
- 멀티테넌트 격리는 1차 원칙(절대원칙 1·2, ADR-008)이나 **collection/RLS/path 게이트는 논리 계층의 격리**일 뿐 — 물리 디스크·백업본에는 무력하다.
- 구체 위험: (a) MinIO data 볼륨/디스크 도난·폐기, (b) `tenant_backup.sh` 백업본 유출(평문), (c) 호스트 파일시스템에 대한 운영자 과다 접근. 이들 시나리오에서 평문이면 **전 테넌트 원본이 동시에 노출**된다.

### 제약

- 인용·audit replay를 위해 답변 시점 원본을 되짚을 수 있어야 한다(절대원칙 10·11). 암호화가 **읽기 경로(parser/재다운로드)를 깨면 안 된다.**
- 폐쇄망 운영(115)은 단일 MinIO 인스턴스다. 무거운 외부 KMS(Vault HA 등)를 전제하지 않고도 배포 가능해야 한다.
- 코드 하드코딩 금지(절대원칙 8) — 암호화 정책은 configs 계층에서 읽는다.

---

## Decision

### 1. 모든 문서 원본은 MinIO **SSE-KMS**로 server-side 암호화한다

`MinIOStorage.save()`는 매 `put_object` 호출에 SSE 명세를 부여한다. **SSE-S3/SSE-C가 아니라 SSE-KMS**를 채택한다:

- **읽기 경로 무변경**: SSE-KMS는 GET 시 클라이언트가 키를 제시할 필요가 없다(서버가 KMS로 복호화). 따라서 parser의 `get_object`/재다운로드 흐름·`delete`/`list_objects`가 그대로 동작한다. (SSE-C는 GET마다 키 헤더가 필요해 읽기 경로를 침습 → 기각.)
- **키 분리**: KMS가 데이터 키를 envelope로 관리 → 원본 바이트와 키가 물리적으로 분리된다. 디스크·백업 도난 시 키 없이는 복호 불가.

### 2. **테넌트별 KMS 키** + **tenant 바인딩 encryption context**

at-rest 계층에서도 멀티테넌트 격리를 1차로 만든다.

- **key_id = `{kms_key_prefix}{domain_id}`** (예: `domainrag-security`). 테넌트마다 별도 KMS 키를 사용해 **암호학적 cross-tenant 격리**를 디스크 계층까지 확장한다.
- **encryption context = `{"tenant_id": <domain_id>}`** 를 항상 부여한다. KMS는 복호화 시 동일 context를 요구(AAD)하므로, **객체가 자기 테넌트에 암호학적으로 바인딩**된다. 키가 단일이어도 context 불일치 복호는 거부된다.

### 3. 정책은 configs/env에서 읽는다 (코드 하드코딩 금지)

`StorageEncryptionPolicy`(아래 §6)는 `configs/platform/storage.yaml`을 단일 진실 소스로 하고, 인프라 연결값은 env로 주입한다.

| 키 | env | 기본 | 의미 |
|---|---|---|---|
| `mode` | `MINIO_SSE_MODE` | `none` | `sse_kms` \| `none` |
| `kms_key_prefix` | `MINIO_SSE_KMS_KEY_PREFIX` | `domainrag-` | key_id 접두 |
| `per_tenant_key` | `MINIO_SSE_PER_TENANT_KEY` | `true` | false면 단일 키(`{prefix}default`) |
| `bind_tenant_context` | `MINIO_SSE_BIND_TENANT_CONTEXT` | `true` | encryption context로 tenant 바인딩 |

- **코드 기본값 `none`**: 어떤 SSE든 MinIO 서버에 KMS 백엔드가 구성돼 있어야 하고, 미구성 상태에서 `sse_kms`로 올리면 `put_object`가 500을 낸다. 따라서 **코드/compose 기본은 `none`**(현 동작 보존)이며, **운영은 KMS 프로비저닝 후 명시적으로 `sse_kms`로 전환**한다(§5).
- 이는 phasing이 아니라 **배포 환경별 config 선택**이다. 본 시스템의 at-rest 암호화 설계는 SSE-KMS 단일이며, `none`은 dev/미프로비저닝 환경의 명시적 설정값이다.

### 4. dev/test 경로 (`LocalFilesystemStorage`)

로컬 dev·단위 테스트의 `LocalFilesystemStorage`는 평문 저장을 유지한다 — **dev 한정**으로 명시한다. 운영 at-rest 암호화 책임은 `MinIOStorage` + MinIO KMS 계층에 있다. (디스크 암호화가 필요한 dev 환경은 볼륨 레벨 LUKS로 처리하며 본 ADR 범위 밖.)

### 5. MinIO KMS 백엔드 프로비저닝 (운영 진실 소스)

`sse_kms` 활성 전 MinIO 서버에 KMS가 구성돼야 한다. 두 가지 배포 형태를 지원한다:

- **per-tenant key (권장, `per_tenant_key=true`)** — MinIO **KES**를 KeyHub(또는 Vault) 백엔드로 띄우고, 테넌트 등록(`tenant_register.sh`) 시 `kes key create domainrag-{domain_id}`로 키를 생성한다. KeyHub를 secret 권위로 재사용(ADR-019 정합).
- **단일 마스터 키 (KES 미구성 환경, `per_tenant_key=false`)** — MinIO 내장 KMS(`MINIO_KMS_SECRET_KEY=domainrag-default:<base64-32B>`)로 at-rest 암호화를 보장한다. 이때 cross-tenant 키 격리는 없으나 **encryption context(tenant_id) 바인딩은 그대로 유효**해 객체-테넌트 결속과 디스크/백업 도난 방어는 확보된다.

신규 테넌트 등록 절차(`tenant_register.sh`)에 KES 키 생성 단계를 추가한다(per_tenant_key 운영 시).

### 6. 구현 형태 (Protocol/Adapter 정합)

- `document_storage.py`에 minio 비의존 **순수 정책 객체**를 둔다:
  - `SseSpec(key_id, context)` — 정책 산출물.
  - `StorageEncryptionPolicy.resolve(domain_id) -> SseSpec | None` — 순수 함수(테스트 용이).
- `MinIOStorage`는 `_build_sse(domain_id)`에서만 minio `SseKMS`로 번역(지연 import)하고 `save()`가 `put_object(..., sse=...)`로 전달한다.
- `deps.py`가 `Settings`에서 `StorageEncryptionPolicy`를 만들어 주입한다.

---

## Consequences

### 좋아지는 점
- 디스크·백업 도난 시 원본이 키 없이는 복호 불가 → 멀티테넌트 격리가 물리 계층까지 확장.
- per-tenant 키 + tenant encryption context로 cross-tenant 복호가 암호학적으로 차단.
- 읽기 경로(parser·인용 replay)·`delete`·`list`는 무변경.
- 정책이 configs/env로 분리되어 운영자가 코드 변경 없이 전환·키 회전 가능.

### 비용·주의
- `sse_kms` 활성 전 MinIO KMS/KES 프로비저닝이 **선행 의무**. 미구성 상태로 전환하면 업로드가 500 — 그래서 기본 `none`.
- 키 분실 = 데이터 복호 불가. KeyHub/KES 백업·키 회전 정책 필요(`key_rotation`은 KES 측 `kes key`/재암호화 절차로, MinIO 객체는 재-PUT 또는 server-side COPY로 재암호화).
- 현재 운영 MinIO 버킷은 비어 있어(객체 0) **기존 평문 객체 마이그레이션 불요**. 최초 업로드부터 암호화 적용.

### 다른 ADR과의 관계
- ADR-008/019(prefix-per-tenant, KeyHub)을 보완(supersede 아님).
- ADR-020(PII·audit)과 정합 — 원본 PII의 at-rest 보호 공백을 메운다.
- `configs/security.yaml`의 `encryption` 블록은 dead config로, 본 ADR이 정의하는 `configs/platform/storage.yaml`이 at-rest 암호화의 단일 진실 소스다.

---

## 참조 구현
- 정책/배선: `backend/app/services/document_storage.py` (`StorageEncryptionPolicy`, `SseSpec`, `MinIOStorage._build_sse`)
- 설정: `configs/platform/storage.yaml`, `backend/app/core/config.py`(`minio_sse_*`), `backend/app/deps.py`
- 테스트: `backend/tests/test_document_storage_sse.py`
