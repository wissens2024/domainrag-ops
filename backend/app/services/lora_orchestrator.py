"""LoRAOrchestrator — KeyHub fetch + vLLM hot-load·unload 결선 (ADR-013 §5, ADR-019 §8).

Activate 흐름:
  1. registry.get(adapter_id) → AdapterRecord (status='registered' 필요)
  2. KeyHub에서 weights bytes fetch (record.keyhub_secret_ref 기준)
  3. vLLM 공유 디렉터리에 임시 파일 저장 (.../<adapter_id>/{...})
  4. vllm_client.load_lora_adapter(lora_name=adapter_id, lora_path=…)
  5. registry.activate(adapter_id) → status='active'
  실패 시 단계별 rollback:
     - vLLM call 실패 → 파일 정리만
     - registry.activate 실패 → vLLM unload + 파일 정리

Retire 흐름:
  1. registry.retire(adapter_id) → status='retired'
  2. vllm_client.unload_lora_adapter (best-effort, swallow on missing)
  3. 임시 파일 정리 (best-effort)

운영 제약:
  - vLLM이 lora_path를 *자신의 로컬 파일시스템*에서 읽으므로 backend↔vLLM이 공유 mount
    (NFS·hostPath·shared volume)를 가져야 한다. 운영 절차는 ADR-019 §8 cluster 토폴로지.
  - dev compose에선 동일 호스트이므로 `vllm_shared_lora_path` 를 단순 디렉터리로 설정.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

from rag_core.interfaces.lora_registry import (
    AdapterRecord,
    LoRAInvalidTransitionError,
    LoRANotFoundError,
    LoRARegistry,
)

logger = logging.getLogger(__name__)


class LoRAOrchestrationError(Exception):
    """KeyHub fetch / vLLM call 실패 — endpoint에서 502 매핑 권장."""


@dataclass
class LoRAOrchestrator:
    """KeyHub + vLLM client + registry 3개를 묶어 activate/retire를 원자성 가깝게 처리."""

    registry: LoRARegistry
    keyhub: object  # KeyHubAdapter (rag_core.interfaces.keyhub.KeyHubAdapter)
    vllm: object | None  # VllmLLMClient or None (inmemory dev에선 vLLM 미운영 가능)
    shared_lora_path: Path

    async def activate(self, *, domain_id: str, adapter_id: str) -> AdapterRecord:
        record = await self.registry.get(domain_id=domain_id, adapter_id=adapter_id)
        if record is None:
            raise LoRANotFoundError(adapter_id)
        if record.status not in ("registered", "active"):
            # retired → active 금지 (registry.activate가 이미 강제하지만 사전 차단)
            raise LoRAInvalidTransitionError(record.status, "active")
        if record.status == "active":
            # idempotent — vLLM 재시동 같은 상황 대비. 다시 load 시도.
            pass

        if self.vllm is None:
            # vLLM 미운영 환경 — registry 상태만 전이 (dev)
            return await self.registry.activate(
                domain_id=domain_id, adapter_id=adapter_id
            )

        # 1) KeyHub에서 weights 가져오기
        if not record.keyhub_secret_ref:
            raise LoRAOrchestrationError(
                f"adapter {adapter_id}: keyhub_secret_ref missing"
            )
        try:
            blob = await self.keyhub.get_secret(record.keyhub_secret_ref)
        except Exception as exc:
            raise LoRAOrchestrationError(
                f"keyhub fetch failed for {record.keyhub_secret_ref}: {exc}"
            ) from exc

        # 2) vLLM 공유 디렉터리에 저장
        adapter_dir = self.shared_lora_path / domain_id / adapter_id
        adapter_dir.mkdir(parents=True, exist_ok=True)
        weights_file = adapter_dir / "adapter_model.bin"
        weights_file.write_bytes(blob)

        # 3) vLLM load
        try:
            await self.vllm.load_lora_adapter(
                lora_name=adapter_id,
                lora_path=str(adapter_dir),
            )
        except Exception as exc:
            # 파일 정리 후 rollback
            try:
                weights_file.unlink(missing_ok=True)
                adapter_dir.rmdir()
            except Exception:  # noqa: BLE001
                pass
            raise LoRAOrchestrationError(
                f"vllm load failed for {adapter_id}: {exc}"
            ) from exc

        # 4) registry 상태 전이
        try:
            activated = await self.registry.activate(
                domain_id=domain_id, adapter_id=adapter_id
            )
        except Exception:
            # vLLM unload + 파일 정리
            try:
                await self.vllm.unload_lora_adapter(lora_name=adapter_id)
            except Exception:  # noqa: BLE001
                pass
            try:
                weights_file.unlink(missing_ok=True)
                adapter_dir.rmdir()
            except Exception:  # noqa: BLE001
                pass
            raise
        return activated

    async def retire(self, *, domain_id: str, adapter_id: str) -> AdapterRecord:
        record = await self.registry.retire(
            domain_id=domain_id, adapter_id=adapter_id
        )
        if self.vllm is not None and record.status == "retired":
            try:
                await self.vllm.unload_lora_adapter(lora_name=adapter_id)
            except Exception as exc:  # noqa: BLE001
                # unload 실패는 swallow — 이미 빠진 어댑터 또는 vLLM 재시작 상황
                logger.warning(
                    "vllm unload_lora_adapter swallowed: %s",
                    exc,
                )
        # 공유 디렉터리는 보존 — 다시 활성화 가능. 운영 cleanup은 별도 cron.
        return record
