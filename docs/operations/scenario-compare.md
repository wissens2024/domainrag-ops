# 시나리오 A vs B 평가 비교 절차

ADR-019 §3·§4 운영 결정용 도구. RTX 3080 10GB × 8 환경에서 모델 매트릭스(시나리오 A:
Qwen 7B-AWQ + 14B-AWQ, 시나리오 B: Qwen 7B fp16 단일)를 평가셋으로 비교해 가성비를
수치로 판단한다.

## 사전 조건

1. **두 vLLM 인스턴스 동시 가동** (또는 시간차 교체 운영)
   - 시나리오 A: `Qwen2.5-7B-Instruct-AWQ` on 115:GPU0+1, `Qwen2.5-14B-Instruct-AWQ` on 174:GPU1+2
   - 시나리오 B: `Qwen2.5-7B-Instruct` (fp16) on 115:GPU0+1, 동일 모델 on 174:GPU1+2
2. **평가셋 준비** — `data/eval/tenants/<tenant>/qa.jsonl` + `citation_gold.jsonl` + `promotion_gate.yaml`
3. **chunks 색인 완료** — 비교 시점에 같은 corpus 상태여야 한다. archival_worker 실행 직후가 권장
4. **AuthFusion 토큰 또는 mock 모드** — `tools/eval_compare.py`는 backend의 orchestrator를 직접 호출하므로 endpoint 인증이 필요하지 않다

## 실행 절차

### 1. 시나리오 A로 vLLM 띄우기

```bash
# 115번 — Tenant SLM (7B-AWQ)
docker run --gpus '"device=0,1"' -p 8000:8000 \
  vllm/vllm-openai \
  --model Qwen/Qwen2.5-7B-Instruct-AWQ \
  --quantization awq \
  --tensor-parallel-size 2 \
  --gpu-memory-utilization 0.85 \
  --enable-lora --max-loras 8

# 174번 — Shared LLM (14B-AWQ)
docker run --gpus '"device=1,2"' -p 8001:8000 \
  vllm/vllm-openai \
  --model Qwen/Qwen2.5-14B-Instruct-AWQ \
  --quantization awq \
  --tensor-parallel-size 2 \
  --gpu-memory-utilization 0.85

# DomainRAG backend env (A)
export TENANT_SLM_BASE_URL=http://115.local:8000/v1
export SHARED_LLM_BASE_URL=http://174.local:8001/v1
```

### 2. 시나리오 A 평가 실행 + 결과 보관

```bash
cd /opt/domainrag
poetry run python -m tools.eval_compare \
  --tenant security \
  --dataset tenant_security \
  --label-a "A: 7B-AWQ + 14B-AWQ" \
  --label-b "B: 7B fp16 단일" \
  --output reports/eval_compare_$(date +%Y%m%d).md
```

위 명령은 같은 endpoint로 두 번 실행하므로 사실상 A만 측정한다. 진짜 비교는 step 3에서.

### 3. 시나리오 B로 vLLM 교체

```bash
# 115번 — 7B fp16
docker run --gpus '"device=0,1"' -p 8000:8000 \
  vllm/vllm-openai \
  --model Qwen/Qwen2.5-7B-Instruct \
  --tensor-parallel-size 2 \
  --gpu-memory-utilization 0.85 \
  --enable-lora --max-loras 8

# 174번 — 같은 7B fp16 (다른 carga용)
docker run --gpus '"device=1,2"' -p 8001:8000 \
  vllm/vllm-openai \
  --model Qwen/Qwen2.5-7B-Instruct \
  --tensor-parallel-size 2

# env는 그대로 둔다 (같은 port에 다른 모델만 띄움)
```

### 4. 시나리오 B 평가 실행

```bash
poetry run python -m tools.eval_compare \
  --tenant security \
  --dataset tenant_security \
  --label-a "B: 7B fp16 단일" \
  --label-b "B: 7B fp16 단일 (repeat)" \
  --output reports/eval_b_$(date +%Y%m%d).md
```

### 5. 결과 비교

A와 B 두 markdown 파일을 펼쳐 다음 metric을 직접 비교한다:

| 항목 | A 기준 | B 채택 조건 |
|---|---|---|
| `citation_accuracy` | 0.90+ | A 대비 손실 ≤ 5%p |
| `unsupported_ratio` | 0.05- | A 대비 증가 ≤ 3%p |
| `fallback_rate` | 0.20- | A 대비 증가 ≤ 5%p |
| `retrieval_recall_at_5` | 변동 없음 | (retrieval 모듈 영향 없음) |
| wall-clock latency | baseline | **A 대비 단축 (가성비 핵심)** |
| `promotion_gate.passed` | `true` 필요 | `true` 필수 |

위 조건을 모두 만족하면 **시나리오 B 채택**, 그렇지 않으면 A 유지.

## 자동화 옵션 (한 번에 비교)

A·B vLLM을 다른 port에 동시 가동할 수 있다면 `tools/eval_compare.py`의 `--override-a` /
`--override-b`로 base_url을 분기:

```bash
poetry run python -m tools.eval_compare \
  --tenant security \
  --dataset tenant_security \
  --override-a '{"model":{"endpoints":{"shared_llm":{"base_url":"http://174.local:8001/v1"}}}}' \
  --override-b '{"model":{"endpoints":{"shared_llm":{"base_url":"http://174.local:8002/v1"}}}}'
```

단, `config_override`는 tenant_config dict에 deep-merge되므로 RAGGraphDeps 내부의 endpoint
client(LLMClient)가 base_url을 그대로 수용해야 한다. 운영 LLMClient가 endpoint를 동적으로
바꾸지 못하면 step 2~4 sequential 방식이 안전하다.

## 운영 결정 기록

비교 완료 후:
1. 결과 markdown을 `reports/`에 commit
2. 채택된 시나리오를 `configs/platform/model.yaml`에 반영 (또는 `configs/platform/scenarios/`에서 symlink)
3. `tenant_lifecycle_logs`에 `model_scenario_promoted` action으로 audit (수동 INSERT 또는
   추후 endpoint 추가)
4. ADR-019 §3·§4 본문 변경 이력 추가
