# vLLM LoRA Hot-swap Runbook (ADR-013 §5 + ADR-019 §8)

LoRA adapter를 KeyHub에서 fetch → vLLM에 hot-load → registry에 active 전이하는 절차.

## 사전 조건

| 항목 | 요구 |
|---|---|
| vLLM | 0.6+ + `--enable-lora --max-loras N` 옵션으로 기동 |
| KeyHub | `KEYHUB_MODE=local`(dev) 또는 `authfusion`(운영) |
| 공유 디스크 | backend ↔ vllm 사이에 `VLLM_SHARED_LORA_PATH` 경로 mount (NFS·hostPath·shared volume) |

## 운영 흐름

1. **Upload (admin UI 또는 API)**:
   ```bash
   curl -X POST http://localhost:8001/api/security/admin/lora/upload \
     -H "Authorization: Bearer <admin>" \
     -F "weights=@security-v1.bin" \
     -F 'metadata={"adapter_id":"security-v1","version":"v1","base_model":"Qwen2.5-7B"}'
   ```
   - LoRA bytes → KeyHub (`put_secret`) → 반환된 `local://lora/security/security-v1/v1` 같은 ref 가 `adapter_registry.keyhub_secret_ref`에 저장
   - status: `registered`

2. **Activate (`LoRAOrchestrator.activate`)**:
   ```bash
   curl -X POST http://localhost:8001/api/security/admin/lora/security-v1/activate \
     -H "Authorization: Bearer <admin>"
   ```
   동작:
   - `keyhub.get_secret(ref)` → bytes
   - `$VLLM_SHARED_LORA_PATH/<tenant>/<adapter_id>/adapter_model.bin` 으로 저장
   - `POST $VLLM/v1/load_lora_adapter` body `{"lora_name":"security-v1","lora_path":"<dir>"}`
   - `registry.activate` → status: `active`
   실패 시 (rollback):
   - vLLM 호출 실패 → 파일 정리만
   - registry.activate 실패 → vLLM unload + 파일 정리

3. **사용 (자동)**: 다음 chat 요청부터 vLLM이 LoRA 적용. `model` 필드에 `lora_name`이 들어가면 자동 라우팅.

4. **Retire**:
   ```bash
   curl -X POST http://localhost:8001/api/security/admin/lora/security-v1/retire \
     -H "Authorization: Bearer <admin>"
   ```
   - `vllm.unload_lora_adapter` (best-effort, swallow on missing)
   - `registry.retire` → status: `retired`
   - 공유 디렉터리는 보존 (재활성화 가능)

## 운영 제약

- vLLM이 `lora_path`를 자기 로컬 파일시스템에서 읽으므로 backend ↔ vLLM 공유 mount 필수
- 운영(prod)은 NFS 또는 k8s PersistentVolume + ReadWriteMany. dev compose는 hostPath bind mount로 충분
- `--max-loras N` 한계 — 이상이면 vLLM이 일부를 LRU evict

## 모니터링

```bash
# vLLM에 등록된 어댑터 목록
curl http://vllm:8000/v1/models

# adapter_registry 상태
SELECT adapter_id, version, status, activated_at, retired_at
  FROM adapter_registry
 WHERE tenant_id = 'security'
 ORDER BY created_at DESC;
```

## 장애 대응

| 증상 | 원인 | 대응 |
|---|---|---|
| `502 lora_orchestration_failed: keyhub fetch failed` | KeyHub down 또는 secret 없음 | KeyHub 헬스체크 + `keyhub_secret_ref` 검증 |
| `502 lora_orchestration_failed: vllm load failed` | vLLM down / max_loras 초과 / lora_path 권한 | vLLM 로그 + `ls -la $VLLM_SHARED_LORA_PATH/<tid>/<aid>/` |
| activate 후 chat이 base model로 응답 | vLLM이 LoRA evict | `--max-loras` 늘리거나 retire-후-activate 재시도 |
| retire 후에도 vLLM 응답에 LoRA | unload 실패(swallow) | 운영자 수동: `POST /v1/unload_lora_adapter` |

## Related

- [ADR-013 §5](../adr/013-model-routing-and-slm-llm-strategy.md) — LoRA serving
- [ADR-019 §8](../adr/019-infrastructure-sharing.md) — KeyHub
- [VllmLLMClient.load_lora_adapter](../../packages/rag_core/rag_core/clients/vllm_client.py)
- [LoRAOrchestrator](../../backend/app/services/lora_orchestrator.py)
