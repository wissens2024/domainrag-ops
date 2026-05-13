"""EvaluationOrchestrator — backend가 rag_core EvalRunner를 호출하는 진입점 (ADR-009 §7).

흐름:
  prepare_run(tenant_id, dataset_name, override) → DB에 job(pending) 생성 + Pending request 큐 적재
  → caller가 BackgroundTask로 execute(job_id) 호출
  → execute가 EvalRunner.run → summary/gate_result를 DB update + status=completed/failed

list_datasets: data/eval/{platform,tenants/<tid>} 디렉토리 스캔.
promote: job을 'promoted' 상태로 전이 + tenant_lifecycle_logs audit. 실제 모델/prompt 승격은
별도 ADR에서 정의될 prompt_registry/model_registry가 담당하므로 본 endpoint는 audit만 남긴다.
"""

from __future__ import annotations

import copy
import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from rag_core.evaluation import (
    EvalRunner,
    PromotionGate,
    load_gate_yaml,
    load_platform_smoke,
    load_tenant_dataset,
)
from rag_core.interfaces.evaluation_job_repository import (
    EvaluationJobRecord,
    EvaluationJobRepository,
)
from rag_core.workflows import RAGGraphDeps

logger = logging.getLogger(__name__)


@dataclass
class PreparedEvaluation:
    job_id: str
    tenant_id: str
    dataset_name: str


class EvaluationOrchestrator:
    """rag_core EvalRunner의 backend wrapper.

    Args:
        deps: 평가에 사용할 RAGGraphDeps (RAGService와 동일 deps 공유 권장)
        repo: EvaluationJobRepository (Postgres 또는 InMemory)
        eval_root: data/eval 디렉토리
    """

    def __init__(
        self,
        *,
        deps: RAGGraphDeps,
        repo: EvaluationJobRepository,
        eval_root: Path,
        ensure_initialized=None,
        ledger_audit=None,
    ) -> None:
        self._deps = deps
        self._repo = repo
        self._eval_root = eval_root
        self._ensure_initialized = ensure_initialized
        self._ledger = ledger_audit
        # job_id → (dataset_name, config_override)
        self._pending: dict[str, tuple[str, dict[str, Any]]] = {}

    @property
    def repo(self) -> EvaluationJobRepository:
        return self._repo

    # ------------------------------------------------------------------ #
    # datasets
    # ------------------------------------------------------------------ #

    def list_datasets(self, tenant_id: str) -> list[dict[str, Any]]:
        """data/eval 디렉토리에서 사용 가능한 dataset 메타 반환.

        반환 형식: [{name, kind, case_count}]. dataset.cases 길이로 case_count 산정.
        """
        out: list[dict[str, Any]] = []

        # platform/smoke.jsonl
        smoke_path = self._eval_root / "platform" / "smoke.jsonl"
        if smoke_path.exists():
            ds = load_platform_smoke(self._eval_root)
            out.append(
                {"name": ds.name, "kind": "platform_smoke",
                 "case_count": len(ds.cases)}
            )

        # tenants/<tid>/qa.jsonl
        tenant_qa = self._eval_root / "tenants" / tenant_id / "qa.jsonl"
        if tenant_qa.exists():
            ds = load_tenant_dataset(self._eval_root, tenant_id)
            out.append(
                {"name": ds.name, "kind": "tenant_qa",
                 "case_count": len(ds.cases)}
            )

        return out

    # ------------------------------------------------------------------ #
    # run
    # ------------------------------------------------------------------ #

    async def prepare_run(
        self,
        *,
        tenant_id: str,
        dataset_name: str,
        actor: str,
        config_override: dict[str, Any] | None = None,
    ) -> PreparedEvaluation:
        """job row(pending) 생성. 호출자가 BackgroundTask로 execute(job_id) 호출."""
        # dataset 검증 — 존재하지 않는 dataset은 즉시 reject
        if not self._resolve_dataset(tenant_id, dataset_name).qa_path.exists():
            raise FileNotFoundError(dataset_name)

        job_id = f"EVAL-{uuid.uuid4().hex[:16].upper()}"
        await self._repo.create(
            EvaluationJobRecord(
                job_id=job_id,
                tenant_id=tenant_id,
                dataset_name=dataset_name,
                status="pending",
                actor=actor,
                config_override=dict(config_override or {}),
            )
        )
        self._pending[job_id] = (dataset_name, dict(config_override or {}))
        return PreparedEvaluation(
            job_id=job_id, tenant_id=tenant_id, dataset_name=dataset_name
        )

    async def execute(self, *, job_id: str, tenant_id: str) -> None:
        """BackgroundTask 진입. _pending에서 dataset+override를 꺼내 EvalRunner 실행."""
        pending = self._pending.pop(job_id, None)
        if pending is None:
            logger.warning("execute called with unknown job_id=%s", job_id)
            return
        dataset_name, override = pending

        # inmemory backend는 RAGService에 seed corpus hook이 있음 — chat 호출이 없으면
        # 자동 실행되지 않으므로 평가 시작 전에 명시적 호출 (caller가 inject한 hook).
        if self._ensure_initialized is not None:
            await self._ensure_initialized()

        await self._repo.update(
            job_id=job_id,
            status="running",
            started_at=datetime.now(timezone.utc),
        )

        try:
            spec = self._resolve_dataset(tenant_id, dataset_name)
            dataset = spec.load()
            runner = EvalRunner(deps=self._deps)
            summary, _per_case = await runner.run(
                dataset, extra_override=override or None
            )

            gate_result: dict[str, Any] = {}
            if spec.gate_path is not None and spec.gate_path.exists():
                gate = load_gate_yaml(spec.gate_path)
                gate_result = gate.evaluate(summary).to_dict()

            await self._repo.update(
                job_id=job_id,
                status="completed",
                summary=summary.to_dict(),
                gate_result=gate_result,
                finished_at=datetime.now(timezone.utc),
            )
        except Exception as exc:  # noqa: BLE001
            logger.exception("evaluation job failed: job_id=%s", job_id)
            await self._repo.update(
                job_id=job_id,
                status="failed",
                error_message=str(exc),
                finished_at=datetime.now(timezone.utc),
            )

    # ------------------------------------------------------------------ #
    # promote
    # ------------------------------------------------------------------ #

    async def promote(
        self,
        *,
        tenant_id: str,
        job_id: str,
        actor: str,
        target: str,
        version: str,
    ) -> EvaluationJobRecord:
        """job을 promoted 상태로 전이. gate를 통과한 경우에만 허용."""
        record = await self._repo.get(tenant_id=tenant_id, job_id=job_id)
        if record is None:
            raise FileNotFoundError(job_id)
        if record.status not in {"completed", "promoted"}:
            raise ValueError(
                f"promote requires completed status, got {record.status}"
            )
        gate_passed = bool(record.gate_result.get("passed"))
        if not gate_passed:
            raise ValueError("promotion_gate_not_passed")

        await self._repo.update(
            job_id=job_id,
            status="promoted",
            promoted_at=datetime.now(timezone.utc),
            promoted_by=actor,
            promotion_target=target,
            promotion_version=version,
        )
        # ADR-020 §8 — Ledger publish. 실패는 swallow.
        if self._ledger is not None:
            try:
                await self._ledger.publish_platform_admin_action(
                    tenant_id=tenant_id, actor=actor,
                    action="evaluation_promoted",
                    details={
                        "job_id": job_id,
                        "target": target,
                        "version": version,
                        "dataset_name": record.dataset_name,
                        "gate_passed": True,
                    },
                )
            except Exception as exc:  # noqa: BLE001
                logger.warning("ledger publish (evaluation_promoted) failed: %s", exc)
        return await self._repo.get(tenant_id=tenant_id, job_id=job_id)  # type: ignore[return-value]

    # ------------------------------------------------------------------ #
    # internals
    # ------------------------------------------------------------------ #

    @dataclass
    class _DatasetSpec:
        kind: str
        qa_path: Path
        gate_path: Path | None
        load: callable  # () -> EvalDataset

    def _resolve_dataset(
        self, tenant_id: str, dataset_name: str
    ) -> "_DatasetSpec":
        """name → loader / paths 매핑.

        지원: "platform_smoke" (data/eval/platform/smoke.jsonl) /
              "tenant_<tid>" (data/eval/tenants/<tid>/qa.jsonl).
        둘 중 어디에도 해당하지 않으면 FileNotFoundError raise — caller가 404.
        """
        if dataset_name == "platform_smoke":
            return self._DatasetSpec(
                kind="platform_smoke",
                qa_path=self._eval_root / "platform" / "smoke.jsonl",
                gate_path=None,
                load=lambda: load_platform_smoke(self._eval_root),
            )
        if dataset_name.startswith("tenant_"):
            tid_for_dataset = dataset_name.removeprefix("tenant_")
            base = self._eval_root / "tenants" / tid_for_dataset
            gate_path = base / "promotion_gate.yaml"
            return self._DatasetSpec(
                kind="tenant_qa",
                qa_path=base / "qa.jsonl",
                gate_path=gate_path if gate_path.exists() else None,
                load=lambda: load_tenant_dataset(self._eval_root, tid_for_dataset),
            )
        # 알 수 없는 dataset_name — caller에서 404 변환 가능하도록 명시적 에러
        raise FileNotFoundError(
            f"unsupported dataset_name: {dataset_name} "
            "(expected 'platform_smoke' or 'tenant_<tid>')"
        )
