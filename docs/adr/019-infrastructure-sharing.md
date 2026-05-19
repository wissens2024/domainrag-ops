# ADR-019: Infrastructure Sharing & Resource Allocation

## Status
**Accepted** (2026-05-08)

> 본 ADR은 DomainRAG Ops가 운영되는 폐쇄망 인프라에서 **AuthFusion Platform / WiSentinel과 공유되는 자원의 분배·격리 정책**을 확정한다. 이전 점검의 critical 이슈 **C3 (RLS 성능)**, **C4 (임베딩 마이그레이션 윈도)**도 본 ADR에 흡수된다.

---

## Context

### 배경 (사실)

- 운영 환경: 폐쇄망. **115번 서버 (2 GPUs)**, **174번 서버 (4 GPUs)** 두 대.
- GPU 사양 (2026-05-12 nvidia-smi 확정):
  - **115번** (`ju@ju-System-Product-Name`): 2 × **NVIDIA GeForce RTX 3080 (10 GB VRAM)**, Driver 570.133.07, CUDA 12.8
  - **174번** (`wissens@wissens-System-Product-Name`): 4 × **NVIDIA GeForce RTX 3080 (10 GB VRAM)**, Driver 570.211.01, CUDA 12.8
- 단일 GPU VRAM이 10GB로 제한되므로 **모든 7B+ 모델은 양자화(AWQ/GPTQ 4-bit) 또는 tensor parallel 분산이 필수**.
- **115번 서버 PostgreSQL 16 인스턴스가 이미 가동 중** (AuthFusion `sso`/`keyhub`/`ledger` schema 보유). 동일 DB 인스턴스 재사용 요구.
- **AuthFusion Platform** (port 8081 SSO, 8085 KeyHub, ...) 이미 운영. ADR-018로 OIDC 연동.
- **WiSentinel** (port 8080 NestJS, OpenSearch + Redis + 115번 GPU 2장 모두 점유)이 174번 GPU 0의 Ollama (Qwen Vision)도 사용. **GS/CC 인증 격리 요구**로 115번 GPU·Redis·OpenSearch는 WiSentinel 전용 유지.
- DomainRAG는 **Qdrant 1.10+ / MinIO / vLLM 두 인스턴스 / bge-m3 임베딩 / cross-encoder reranker** 신규 도입.
- 이전 점검:
  - **C3**: PostgreSQL RLS 성능 영향(통상 10-30%) 미논의
  - **C4**: 운영자가 `tenants.embedding_model` 변경 → alias swap 완료까지 inconsistency window
- ADR-013이 Tenant SLM (multi-tenant + LoRA)과 Shared LLM 두 vLLM 인스턴스 결정 — 단, 본 ADR §3 재조정으로 두 endpoint가 한 vLLM instance를 가리킨다 (GPU 부족).
- **사용자 결정 (2026-05-12 WiSentinel 협의 후 최종)**:
  - **174번 GPU 4장 = DomainRAG 전용** (GPU 0 Ollama는 공유 유지)
  - **115번 GPU 2장 = WiSentinel 전용** — DomainRAG GPU 미사용
  - **OpenSearch / Redis = WiSentinel 전용** — DomainRAG는 두 시스템 모두 의존하지 않음
  - PostgreSQL은 115번 인스턴스를 데이터베이스 분리로 공유 (§1)

### 가정

- 두 서버 간 네트워크 latency 충분히 낮음 (<5ms 같은 LAN/VLAN)
- PostgreSQL 16이 RLS·BYPASSRLS 모두 정상 동작
- KeyHub API가 안정적 가용 (TLS, API key 인증)
- Qdrant·MinIO는 두 서버 어느 쪽에 배치해도 무방 (Compose stack 단일)
- 174번 GPU 0의 Ollama는 WiSentinel·DomainRAG가 공유 가능 (모델 로드 보존, RPS 충돌 없음)

가정 깨지면 재검토 — 특히 LAN latency 큼·Ollama 공유 부하 충돌은 GPU 재분배 트리거.

---

## Decision

### 1. PostgreSQL — 같은 인스턴스, 별도 database

```text
PostgreSQL 16 (115번)
  ├─ database: aines           (AuthFusion 기존)
  │   ├─ schema sso
  │   ├─ schema keyhub
  │   └─ schema ledger
  └─ database: domainrag       ← 신규
      ├─ table tenants, user_tenant_membership, ...
      ├─ table chunks_*, documents_*, ...  (RLS 적용)
      └─ table chat_logs, indexing_jobs, ...
```

- **별도 database 권장** (schema 분리보다 격리 강함). backup·migration·user/role 분리 단순.
- DomainRAG 전용 PostgreSQL role 2개:
  - `domainrag_app` — 일반 application 연결, RLS 적용
  - `domainrag_platform_admin` — BYPASSRLS 속성 보유, cross-tenant 통계용
- AuthFusion·DomainRAG의 PostgreSQL user 권한 격리 (CREATE DATABASE 등 cross 작업 금지).

### 2. PostgreSQL RLS 성능 가이드라인 (C3 흡수)

RLS는 통상 query 성능 10-30% 저하. 다음 가이드라인을 의무화:

```sql
-- 모든 tenant-scoped 테이블에 (tenant_id, ...) 복합 인덱스 선두
CREATE INDEX idx_chunks_tenant_doc ON chunks (tenant_id, doc_id, doc_version, parser_version);
CREATE INDEX idx_chat_logs_tenant_time ON chat_logs (tenant_id, created_at DESC);
CREATE INDEX idx_indexing_jobs_tenant_status ON indexing_jobs (tenant_id, status);
-- ... 모든 핵심 테이블

-- RLS policy는 단순 비교만 사용 (함수 호출 회피)
CREATE POLICY tenant_isolation ON chunks
  USING (tenant_id = current_setting('app.current_tenant')::text);
```

- **RLS policy는 함수 호출(예: `get_current_tenant()`) 회피** — `current_setting` 직접. planner가 인덱스 활용 가능.
- chat_logs는 **시간 기반 partitioning** 의무 (`PARTITION BY RANGE (created_at)` 월 단위) — 대형 테넌트의 RLS scan 비용 제한.
- 매 connection 생애 시작에 `SET LOCAL app.current_tenant = '<tenant_id>'` (FastAPI dependency가 책임).
- BYPASSRLS는 **platform_admin role 한정** (cross-tenant 분석 한정), 일반 app 연결은 절대 BYPASSRLS 미사용.
- 정기 EXPLAIN ANALYZE 점검 — slow query 발견 시 인덱스 보강 또는 partitioning 강화.

#### 수치 정밀도 규칙 (CLAUDE.md Y10)

similarity·confidence·score 종류의 부동소수 값은 다음을 의무화한다.

- PostgreSQL 컬럼 type은 `DOUBLE PRECISION` (FLOAT의 별명, 8byte). `REAL`/`FLOAT4` 금지 — 4byte로는 cosine similarity 누적 오차가 임계치 비교에 영향.
- pydantic 모델·dataclass의 Python type은 `float` (CPython은 double 동등).
- API 응답 JSON은 **소수점 4자리 round** (`round(value, 4)`). 정렬·표시·테스트 비교 모두 4자리 기준.
- 내부 비교/임계치 평가는 raw double 값으로 수행한 뒤 round는 출력 단계에서만 적용 — 임계치 경계(0.55/0.75 등) 정확도 보존.

### 3. GPU 매핑 (운영 확정 — 2026-05-12 WiSentinel 협의 반영)

DomainRAG는 **174번 GPU 4장만 사용**한다. 115번 GPU 2장은 WiSentinel 전용(GS/CC 격리).
RTX 3080 10GB 단일 카드 제약 + 174번 4장 한계로 **Tenant SLM과 Shared LLM은 한 vLLM
instance를 공유**한다 (system prompt + LoRA 분기). 14B Shared LLM은 본 매핑에서 운영 불가.

```text
[174번 서버 — wissens@wissens-System-Product-Name, 4 × RTX 3080 10GB] — DomainRAG 전용 (GPU 0은 공유)
  GPU 0  Ollama (Qwen2-VL 7B q4_K_M gguf)
         · AuthFusion·WiSentinel과 공유 (모델 캐시 공유, RPS 모니터링 의무)
         · DomainRAG는 vision OCR (PDF 이미지 → 텍스트)에 선택적 활용
  GPU 1  Tenant + Shared LLM 통합 vLLM (Qwen 7B-AWQ + multi-LoRA, tensor parallel size=2)
  GPU 2  위와 tensor parallel (KV cache + max_loras=8 여유)
         · 한 instance가 두 endpoint(`tenant_slm`, `shared_llm`)를 alias로 서비스
         · Tenant 호출은 LoRA per-request, Shared 호출은 LoRA 없이 baseline
         · inference_judge / query_rewrite 모두 같은 instance를 system prompt로 분기
         · fp16 14GB → AWQ 4-bit ≈ 4GB/카드 + LoRA + KV cache, util 0.85 권장
  GPU 3  Embedding + Reranker
         · bge-m3 (dense+sparse, fp16 ~2GB)
         · bge-reranker-v2-m3 (fp16 ~2GB)
         · 동시 로드 ~5GB

[115번 서버 — ju@ju-System-Product-Name, 2 × RTX 3080 10GB] — WiSentinel 전용
  GPU 0, 1  WiSentinel
            · DomainRAG는 본 GPU에 워크로드 배치하지 않음 (GS/CC 격리)
```

- **본 매핑의 핵심 트레이드오프**:
  1. *latency 격리 약화* — judge·rewrite·user chat이 같은 vLLM instance를 경유. judge가
     무거우면 user chat latency에 영향. ADR-013 §7 fallback_chain으로 timeout 회피.
  2. *14B reasoning 손실* — Shared LLM이 14B → 7B로 강등. inference judge 정확도 회귀
     가능. ADR-009 §7 `data/eval/tenants/<id>/promotion_gate.yaml`로 사전 검증 의무
     (`tools/eval_compare.py` 시나리오 B 절차).
  3. *blast radius 증가* — 174:GPU1·2 vLLM 단일 장애 시 모든 LLM 호출 차단. 115번 fallback
     vLLM 없음. 운영 monitoring 강화 필요.
- 174 GPU 0 Ollama는 모델 캐시 공유라 WiSentinel·DomainRAG 동시 사용 가능 (RPS 모니터링).
- **공통 제약 — 10GB 단일 카드는 fp16 7B 이상 모델 단독 호스팅 불가**. 모든 vLLM은
  AWQ 4-bit + tensor parallel 2 결합 필수.

### 4. vLLM 인스턴스 운영 정책

```yaml
# configs/platform/model.yaml (발췌)
endpoints:
  # 174 GPU 1+2의 단일 vLLM instance를 두 endpoint가 alias로 사용. tenant 호출은
  # multi-LoRA per-request, shared 호출은 LoRA 없는 baseline + 별도 system prompt.
  tenant_slm:
    base_url: http://174.local:8000/v1
    backend: vllm
    base_model: Qwen2.5-7B-Instruct-AWQ      # 4-bit 양자화 (10GB×2 카드 제약)
    quantization: awq
    multi_lora: true
    max_loras: 8
    tensor_parallel_size: 2
    gpu_memory_utilization: 0.85             # 4-bit + LoRA + KV cache 여유 확보
  shared_llm:
    base_url: http://174.local:8000/v1       # tenant_slm과 동일 instance (GPU 부족으로 통합)
    backend: vllm
    base_model: Qwen2.5-7B-Instruct-AWQ      # 14B-AWQ는 본 매핑에서 운영 불가 — 7B baseline 사용
    quantization: awq
    multi_lora: false                        # judge/rewrite는 LoRA 없는 baseline
    tensor_parallel_size: 2
    gpu_memory_utilization: 0.85
    note: "tenant_slm alias — 별도 vLLM instance 아님. system prompt로 분기"
  ollama_vision:
    base_url: http://174.local:11434
    backend: ollama
    model: qwen2-vl-7b-instruct-q4_K_M       # Ollama gguf 4-bit (10GB 단일 카드)
    shared_with: [authfusion, wisentinel]
embedding:
  base_url: http://174.local:8002      # GPU 3 전용 inference server (TEI 또는 자체)
  model: bge-m3                        # fp16 ~2GB
reranker:
  base_url: http://174.local:8003
  model: bge-reranker-v2-m3            # fp16 ~2GB, embedding과 GPU 3 공유
```

- 모든 endpoint는 OpenAI-compatible API 또는 표준 inference server (Ollama / TEI).
- Health check: `/health`로 30초마다 점검 (ADR-013 endpoint health dashboard).
- 양자화는 운영 기본값. fp16 운영을 원하면 더 큰 VRAM(예: A100 40GB) GPU로 교체해야 한다.

### 5. 임베딩 모델 변경 운영 절차 (C4 흡수)

운영자가 `configs/tenants/<tenant_id>/model.yaml`의 `embedding.model`을 직접 toggle하는 것은 **금지**. 대신 명시적 워크플로우:

```text
임베딩 모델 변경 워크플로우 (ADR-012 §7 보강)

Step 1) Admin UI "임베딩 모델 변경" 버튼 클릭
        → POST /api/{tenant_id}/admin/embedding/migrate
                 { "from": "bge-m3", "to": "bge-m4", "validation_eval_id": "..." }
Step 2) 새 collection 생성 (`chunks_<tenant_id>_v2`)
Step 3) Batch 재인덱싱 (모든 chunk를 새 모델로 dense+sparse 재생성)
Step 4) 평가 셋으로 검증 (새 collection vs 기존 collection 검색 품질 비교)
Step 5) 운영자 명시 승인 (UI에서 confirm)
Step 6) Atomic alias swap (`chunks_<tenant_id>` alias → `chunks_<tenant_id>_v2`)
Step 7) 기존 collection 30일 보관 (rollback 가능 윈도)
Step 8) 30일 후 platform_admin 명시 삭제

원칙:
  - tenants.embedding_model 컬럼은 alias가 가리키는 실제 collection의 모델로 자동 동기화 (Step 6 시점)
  - Step 1~7 사이 사용자 query는 항상 기존 모델로 처리 (inconsistency window 0)
  - 평가 검증 실패 시 Step 6 진행 안 됨 (자동 abort)
  - 마이그레이션 중 indexing_jobs는 일시 정지 (race condition 방지)
```

`tenants` 테이블에 `embedding_migration_state` 컬럼 추가 (`idle | preparing | validating | swapping | completed | failed`). 사용자 query는 `idle`/`completed`/`failed` 상태에서만 처리.

### 6. Qdrant 배치

- 단일 Qdrant cluster (또는 single-node) — 어느 서버든 무방. 통상 115번 (DB와 같은 서버)에 배치해 cross-server traffic 최소화.
- collection-per-tenant (ADR-008·011 정합).
- backup은 snapshot API + per-tenant collection export (`infra/scripts/tenant_backup.sh`).

### 7. MinIO 배치

- 단일 MinIO instance, prefix-per-tenant.
- 디스크 공간이 큰 서버에 배치. 기본 115번 권장 (DB와 함께 운영 단순).

### 8. KeyHub 활용

DomainRAG는 KeyHub을 다음에 사용:

```text
1) LoRA adapter weights 보관
   - upload 시 envelope encrypt → KeyHub `POST /keys`
   - 사용 시 `GET /keys/{id}` 복호화하여 vLLM에 로드
2) AuthFusion service account secret 보관
3) 외부 endpoint API key 보관 (예: 외부 평가 데이터셋 다운로드 토큰)

API Key 인증:
  - DomainRAG 자체 API Key (KeyHub의 `X-API-Key` 헤더)
  - configs/platform/keyhub.yaml에 endpoint·api_key 명시
  - api_key 자체는 환경변수 또는 .env (gitignore)
```

`KeyHubAdapter` Protocol 신설 (`packages/rag_core/interfaces/`)로 추상화. 운영 모드는 환경변수 `KEYHUB_MODE`로 분기.

#### KeyHub 운영 모드·환경변수 명세

| 환경변수 | 값 | 의미 |
|---|---|---|
| `KEYHUB_MODE` | `authfusion` (운영) / `local` (dev·CI·staging) | 구현체 선택 |
| `KEYHUB_ENDPOINT` | URL | AuthFusion KeyHub 주소 (mode=authfusion 한정) |
| `KEYHUB_API_KEY` | secret | KeyHub 인증 (mode=authfusion 한정) |
| `KEYHUB_LOCAL_PATH` | 파일시스템 경로 | `LocalSecretStore`의 secret blob 디렉터리 (mode=local 한정) |
| `KEYHUB_LOCAL_FERNET_KEY` | base64 url-safe 32-byte | `LocalSecretStore` 암호화 키 (None이면 평문 저장, **dev 한정**) |

**운영 진실 소스**: 폐쇄망 운영(production)은 `KEYHUB_MODE=authfusion` 의무. `local` 모드는 dev compose·CI·staging·단독 데모용 — 운영 환경 배포 시 `AuthFusionKeyHub` 구현체로 교체. backend `get_keyhub_adapter` deps가 분기.

**구현체 진행 현황 (2026-05-15)**:
- `LocalSecretStore` (`rag_core/clients/local_secret_store.py`) — dev fallback 운영 가능
- `AuthFusionKeyHub` — SSO 결선 ([ADR-018](./018-sso-integration-authfusion.md)) 완료 후 별도 작업으로 추가 (현재 deps는 `KEYHUB_MODE=authfusion` 시 `NotImplementedError`)

**LoRA 결선** (ADR-013 §5): LoRA adapter weights는 backend `upload_lora_adapter` endpoint가 KeyHub에 저장 → `adapter_registry.keyhub_secret_ref` 컬럼에 ref URI 보관 → `LoRAOrchestrator.activate`가 fetch 후 `VLLM_SHARED_LORA_PATH` 경로에 파일로 write → `vLLM /v1/load_lora_adapter` 호출.

### 9. Ollama 공유 정책

- 174번 GPU 0의 Ollama는 stateless inference라 동시 사용 가능
- 단, **RPS·QPS 충돌 모니터링** 필수 — Prometheus metric으로 측정
- DomainRAG가 Ollama 호출하는 use case:
  - vision OCR (PDF 이미지 페이지 → 텍스트, ADR-012 indexing 시)
  - inference judge fallback (Shared LLM 장애 시 fallback chain의 마지막 단계, ADR-013)
- Ollama가 응답 못 하면 timeout 후 다른 endpoint로 fallback (ADR-013 model_failure_chain)

### 10. 네트워크 / 보안

- 모든 inter-service traffic은 TLS (자체 CA 운영) 또는 폐쇄망 VLAN에서 평문 허용
- AuthFusion JWKS·token endpoint는 TLS 필수
- KeyHub API는 TLS + API Key 둘 다 필수
- Qdrant·MinIO·PostgreSQL은 폐쇄망 내부 신뢰 zone (방화벽으로 외부 차단)
- vLLM endpoint는 internal-only

### 11. CPU 서비스 배치 + 미사용 자원 명시 (2026-05-12 WiSentinel 협의)

**CPU 서비스 배치 자유**: 모든 CPU 서비스(FastAPI backend, archival_worker, Next.js
frontend 등)는 port 중복이 없으면 115/174 어느 서버에든 배치 가능. 운영자 자율.

| 서비스 | 권장 위치 | 비고 |
|---|---|---|
| PostgreSQL 16 | 115번 | AuthFusion 기존 인스턴스 재사용 (§1) |
| Qdrant 1.10+ | 115번 권장 | DB 인접 — cross-server traffic 최소화 |
| MinIO | 115번 권장 | 디스크 여유 |
| FastAPI backend | 자유 | 외부 reverse proxy 뒤 |
| archival_worker | 자유 | cron 단일 인스턴스 |
| Next.js frontend | 자유 | reverse proxy 뒤 |

**DomainRAG가 사용하지 않는 자원** (WiSentinel 전용):

- **OpenSearch** — DomainRAG는 의존 없음. vector store는 Qdrant, full-text는 chat_logs JSONB.
- **Redis** — DomainRAG는 현재 ADR 기준 의존 없음. OAuth2 PKCE state store는 InMemory로 충분
  (단일 backend 인스턴스 가정). chat_logs partitioning cron의 lock은 PostgreSQL
  `pg_try_advisory_lock`으로 대체. 운영팀이 backend 다중 인스턴스 도입을 결정하면 그 시점에
  별도 ADR로 Redis 의존성 정식화.
- **115번 GPU** — WiSentinel 전용. DomainRAG 워크로드 배치 금지.

---

## Consequences

### 긍정적 영향

- 인프라 추가 최소 (PostgreSQL 재사용, Ollama 공유)
- AuthFusion·KeyHub과 보안 표준 일치 (TLS·API Key·JWT)
- GPU 분배가 latency·격리·가용성 균형 — Tenant SLM/Shared LLM/Embedding/Vision 4개 독립
- C3·C4 critical 이슈 본 ADR로 해결
- 임베딩 마이그레이션을 명시 워크플로우화로 운영 사고 차단

### 부정적 영향 / 부채

- PostgreSQL 단일 인스턴스 = single point of failure (DR은 ADR-021 별도, 본 ADR 범위 외)
- 174번 GPU 0 Ollama 공유 = WiSentinel/DomainRAG 부하 충돌 시 trade-off (모니터링으로 조기 감지)
- KeyHub 가용성 의존 — 장애 시 LoRA 신규 로드 차단 (기존 캐시 LoRA는 운영 지속)
- GPU 0 Ollama가 부하 폭증할 경우 vision OCR 배치 부담 (재분배 필요)
- chat_logs partitioning 운영 부담 (월별 자동 생성 cron)
- **VRAM 10GB 제약** — 모든 LLM이 4-bit 양자화 의존. 양자화로 인한 품질 저하(통상 0.5-1%p)는 평가셋(ADR-009 §7)으로 회귀 감시 필요. fp16 운영이 필요해지면 GPU 업그레이드 또는 추가 도입 트리거.
- **115번 GPU 0장 — 모든 LLM 워크로드가 174번 단일 서버에 집중**. 174번 LLM vLLM 장애 시
  fallback vLLM 없음(115번에 backup 배치 불가). HA 요구 시 별도 ADR로 174번 추가 또는
  115번 협상 재개 필요.
- **Tenant·Shared LLM endpoint 통합** — latency 격리 약화. ADR-013이 가정한 endpoint 독립
  운영 가정이 깨지며, 한 모델 instance가 multi-LoRA + baseline 호출을 동시에 처리. 부하 spike
  시 user chat과 judge가 같은 큐를 공유해 tail latency 증가 가능.
- **14B Shared LLM 운영 불가** — inference judge가 7B baseline로 강등. ADR-010 §4 LLM-as-judge
  정확도가 14B 대비 5-10%p 회귀할 수 있어 `tools/eval_compare.py`로 사전 검증 의무.

### 후속 작업

- `configs/platform/model.yaml` 신설 (위 schema)
- `configs/platform/keyhub.yaml` 신설
- `configs/platform/postgres.yaml` 신설 (RLS 가이드라인 + connection pool)
- PostgreSQL `domainrag` database 생성 + role 분리 + RLS migration
- chat_logs partitioning migration (월 단위)
- KeyHubAdapter Protocol + 구현체 (`packages/rag_core/`)
- TEI(Text Embeddings Inference) 또는 자체 Embedding/Reranker 서버 셋업 (174번 GPU 3)
- 174번 GPU 0 Ollama 사용량 monitoring (Prometheus)
- ADR-012 §7 임베딩 마이그레이션 절차에 본 ADR §5 절차 cross-reference 보강
- **AWQ/GPTQ 4-bit 양자화 모델 가용성 검증** — Qwen2.5-7B-Instruct-AWQ 폐쇄망 반입 + vLLM 호환성 확인 (14B-AWQ는 본 매핑에서 운영 불가하므로 제외)
- **양자화 회귀 평가** — fp16 baseline 대비 정확도 손실을 `data/eval/tenants/<id>/qa.jsonl`로 측정 (ADR-009 §7 promotion_gate)
- **VRAM 모니터링** — 10GB 카드는 OOM 여유가 적음. gpu_memory_utilization=0.85 + nvidia-smi metric 알림 의무
- **`tools/eval_compare.py` 시나리오 B 검증** — judge/rewrite의 7B baseline 운영이 platform_smoke + tenant_security promotion_gate 통과하는지 확인. 미통과 시 ADR 재논의(115번 GPU 협상 또는 174번 추가 도입)
- **174번 vLLM HA 부재 문제** — 단일 장애점이므로 health monitoring · 자동 재시작 · process supervisor 의무

---

## Alternatives Considered

### 1. PostgreSQL 별도 인스턴스
- **장점**: AuthFusion과 완전 격리
- **기각 사유**: 사용자 요구(같은 DB 재사용), 운영 부담 ↑.

### 2. database 같이 사용, schema만 분리 (`aines.domainrag` schema)
- **장점**: connection pool 공유
- **기각 사유**: backup/migration 격리 약함. 별도 database가 운영 단순성 우수.

### 3. RLS 미적용, 응용 단에서만 tenant filter
- **장점**: 성능 부담 0
- **기각 사유**: ADR-008 격리 3중 방어 위반. RLS는 안전망이라 절대 포기 불가.

### 4. 174번 GPU 모두 vLLM, 175번 별도 임베딩 서버 도입
- **장점**: 자원 단일 도메인
- **기각 사유**: 추가 서버 도입 부담. GPU 분배만으로 충분.

### 5. Ollama를 DomainRAG도 별도 인스턴스 운영
- **장점**: 부하 충돌 0
- **기각 사유**: 모델 메모리 중복, GPU 자원 낭비.

### 6. KeyHub 미사용, DomainRAG 자체 secret 관리
- **장점**: 외부 의존 0
- **기각 사유**: AuthFusion 생태계 보안 표준 부합, audit 통합. 자체 구현은 보안 위험.

### 7. 임베딩 모델 변경을 admin UI 단순 toggle로 허용
- **장점**: UX 단순
- **기각 사유**: C4 inconsistency window 발생. 운영 사고 위험 (ADR-012 §7도 alias swap 명시).

---

## Related

- [ADR-008: Multi-Tenant Architecture](./008-multi-tenant-architecture.md) — RLS·tenant 격리
- [ADR-009: Tenant Control Plane](./009-tenant-control-plane.md) — configs hybrid (filesystem + DB)
- [ADR-011: Hybrid Retrieval v2](./011-hybrid-retrieval-v2.md) — bge-m3·DBSF
- [ADR-012: Lifecycle v2](./012-lifecycle-v2.md) — alias swap (본 ADR §5가 보강)
- [ADR-013: Model Routing](./013-model-routing-and-slm-llm-strategy.md) — vLLM Tenant/Shared 분리
- [ADR-018: SSO Integration](./018-sso-integration-authfusion.md) — AuthFusion 의존
- [ADR-020: PII & Audit Integration](./020-pii-and-audit-integration.md) — WiSentinel 룰 활용
- AuthFusion `docs/architecture/` (외부)

---

**작성자**: AI Assistant + Project Owner  
**최종 승인**: 2026-05-08

**변경 이력**:
- 2026-05-08: 초안 작성, 즉시 Accepted (C3·C4 흡수)
- 2026-05-12: §배경·§3·§4 GPU 사양 정정 (nvidia-smi 확정값 반영) — 115/174번 모두 RTX 3080 10GB. AWQ 4-bit 양자화 + tensor parallel 필수 결정 추가. §Consequences에 VRAM 부채 + 양자화 회귀 평가 후속 작업 추가
- 2026-05-12: WiSentinel 협의 결과 반영 — §3 GPU 매핑 재조정 (115번 WiSentinel 전용 / 174번 4장 DomainRAG / Tenant·Shared LLM 통합 vLLM). §4 endpoint 통합. §11 신설 (CPU 서비스 자유 배치 + OpenSearch/Redis DomainRAG 미사용 명시). 14B Shared LLM 운영 불가 결정 + 시나리오 B 사실상 강제
