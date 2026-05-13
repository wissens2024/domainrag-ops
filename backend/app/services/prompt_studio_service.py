"""PromptStudioService — ADR-017 §12 prompts CRUD + preview.

흐름:
  - list_tasks(tenant_id): platform prompts/*.yaml 스캔 → task name 목록
  - get_prompt(tid, task): platform default + runtime override 합성
  - patch(tid, task, version, ab_slot, system?, user?): runtime override 저장 + history
  - preview(tid, task, system?, user?, sample_question, contexts?): Jinja2 렌더 + 선택 LLM 호출

저장:
  - in-process dict (PromptStudioService._runtime_overrides) — 같은 프로세스 내 즉시 반영
  - history는 별도 in-process 리스트 (audit는 추후 ADR-009 §5 DB→load 영구화 작업에서 통합)

PATCH 시 GenerationPrompt cache 무효화 — 다음 chat 호출이 새 prompt를 로드해야 한다.
현재 GenerationService는 호출마다 자체 prompt를 들고 있으므로(생성자 주입) 변경이 chat
flow에 즉시 반영되려면 GenerationService 재구성 트리거가 필요. 본 작업의 책임 밖이며
follow-up으로 명시.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined


_PREVIEW_JINJA = Environment(undefined=StrictUndefined, autoescape=False)


@dataclass
class PromptRecord:
    task: str
    version: str
    ab_slot: str
    system: str
    user: str
    schema_version: int | None = None
    response_schema_path: str | None = None
    source: str = "platform"  # 'platform' | 'tenant_runtime'
    updated_at: datetime | None = None
    updated_by: str | None = None
    reason: str | None = None


@dataclass
class PromptChangeRecord:
    tenant_id: str
    task: str
    version: str
    ab_slot: str
    old: PromptRecord | None
    new: PromptRecord
    changed_at: datetime
    changed_by: str | None
    reason: str | None


class PromptStudioService:
    """Prompt 목록·읽기·갱신·preview. 같은 프로세스 in-memory override.

    Args:
        config_dir: configs/ root (settings.config_dir)
        llm_client: 선택 (preview에서 sample_answer 산출)
    """

    # 모든 tenant 공유 in-process override store. Tenant 격리는 첫번째 키로 보장.
    _runtime: dict[tuple[str, str, str, str], PromptRecord] = {}
    _history: list[PromptChangeRecord] = []

    def __init__(self, *, config_dir: Path, llm_client=None) -> None:
        self._config_dir = config_dir
        self._llm = llm_client

    # ---------------------------------------------------------------- listing

    def list_tasks(self, tenant_id: str) -> list[PromptRecord]:
        """platform/prompts/*.yaml 파일을 task 단위로 묶어 노출.

        같은 task의 runtime override가 있으면 그것이 우선.
        """
        records: dict[tuple[str, str, str], PromptRecord] = {}

        # 1) platform default
        platform_dir = self._config_dir / "platform" / "prompts"
        if platform_dir.exists():
            for yaml_path in sorted(platform_dir.glob("*.yaml")):
                rec = _load_yaml_record(yaml_path)
                if rec is not None:
                    records[(rec.task, rec.version, rec.ab_slot)] = rec

        # 2) runtime override (tenant scope)
        for key, rec in self._runtime.items():
            if key[0] == tenant_id:
                records[(key[1], key[2], key[3])] = rec

        return sorted(
            records.values(),
            key=lambda r: (r.task, r.version, r.ab_slot),
        )

    def get_prompt(
        self,
        tenant_id: str,
        task: str,
        version: str | None = None,
        ab_slot: str | None = None,
    ) -> PromptRecord | None:
        candidates = [
            r for r in self.list_tasks(tenant_id) if r.task == task
        ]
        if version is not None:
            candidates = [r for r in candidates if r.version == version]
        if ab_slot is not None:
            candidates = [r for r in candidates if r.ab_slot == ab_slot]
        return candidates[0] if candidates else None

    # ---------------------------------------------------------------- patch

    def patch(
        self,
        *,
        tenant_id: str,
        task: str,
        version: str,
        ab_slot: str,
        system: str | None,
        user: str | None,
        actor: str | None,
        reason: str | None,
    ) -> PromptChangeRecord:
        """system 또는 user 부분 갱신. 적어도 한쪽은 주어져야 한다.

        raises:
            ValueError: 둘 다 None이면 'empty_patch'
        """
        if system is None and user is None:
            raise ValueError("empty_patch")

        existing = self.get_prompt(tenant_id, task, version, ab_slot)
        # 새 record — base는 existing(platform 또는 runtime). 없으면 빈 record로 시작.
        if existing is None:
            base = PromptRecord(
                task=task, version=version, ab_slot=ab_slot,
                system="", user="",
            )
        else:
            base = existing

        new_record = PromptRecord(
            task=base.task,
            version=base.version,
            ab_slot=base.ab_slot,
            system=system if system is not None else base.system,
            user=user if user is not None else base.user,
            schema_version=base.schema_version,
            response_schema_path=base.response_schema_path,
            source="tenant_runtime",
            updated_at=datetime.now(timezone.utc),
            updated_by=actor,
            reason=reason,
        )

        key = (tenant_id, task, version, ab_slot)
        self._runtime[key] = new_record

        change = PromptChangeRecord(
            tenant_id=tenant_id,
            task=task, version=version, ab_slot=ab_slot,
            old=existing,
            new=new_record,
            changed_at=new_record.updated_at,
            changed_by=actor,
            reason=reason,
        )
        self._history.append(change)
        return change

    def list_history(
        self, *, tenant_id: str, task: str | None = None,
        limit: int = 50, offset: int = 0,
    ) -> list[PromptChangeRecord]:
        out = [h for h in self._history if h.tenant_id == tenant_id]
        if task is not None:
            out = [h for h in out if h.task == task]
        out.reverse()
        return out[offset : offset + limit]

    # ---------------------------------------------------------------- preview

    async def preview(
        self,
        *,
        tenant_id: str,
        task: str,
        system: str | None,
        user: str | None,
        sample_question: str,
        sample_contexts: list[dict[str, Any]] | None = None,
        invoke_llm: bool = False,
    ) -> dict[str, Any]:
        """Jinja2 렌더 + 선택적으로 LLM 호출해 sample_answer 생성.

        system/user 미지정 시 task의 현재 effective 사용.
        sample_contexts 미지정 시 1건 dummy.
        """
        base = self.get_prompt(tenant_id, task)
        sys_tmpl = system if system is not None else (base.system if base else "")
        user_tmpl = user if user is not None else (base.user if base else "")
        contexts = sample_contexts or [
            {
                "title": "샘플 문서",
                "page_number": 1,
                "section_title": "샘플 섹션",
                "content": "샘플 본문입니다. 실제 답변 생성 시에는 retrieval 결과가 들어갑니다.",
            }
        ]
        try:
            rendered_system = _PREVIEW_JINJA.from_string(sys_tmpl).render(
                question=sample_question, contexts=contexts
            )
            rendered_user = _PREVIEW_JINJA.from_string(user_tmpl).render(
                question=sample_question, contexts=contexts
            )
        except Exception as exc:  # noqa: BLE001
            return {
                "rendered_system": None,
                "rendered_user": None,
                "render_error": str(exc),
                "sample_answer": None,
            }

        sample_answer: str | None = None
        if invoke_llm and self._llm is not None:
            try:
                full_prompt = f"{rendered_system}\n\n{rendered_user}"
                sample_answer = await self._llm.generate(
                    full_prompt,
                    model="tenant_slm",
                    max_tokens=512,
                    temperature=0.2,
                )
            except Exception as exc:  # noqa: BLE001
                sample_answer = None

        return {
            "rendered_system": rendered_system,
            "rendered_user": rendered_user,
            "render_error": None,
            "sample_answer": sample_answer,
        }

    # --------------------------------------------------------- test helpers

    @classmethod
    def reset(cls) -> None:
        cls._runtime.clear()
        cls._history.clear()


def _load_yaml_record(path: Path) -> PromptRecord | None:
    try:
        with path.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
    except Exception:  # noqa: BLE001
        return None
    name = data.get("name")
    if not name:
        return None
    return PromptRecord(
        task=str(name),
        version=str(data.get("version", "v1")),
        ab_slot=str(data.get("ab_slot", "control")),
        system=str(data.get("system", "")),
        user=str(data.get("user", "")),
        schema_version=data.get("schema_version"),
        response_schema_path=data.get("response_schema_path"),
        source="platform",
    )
