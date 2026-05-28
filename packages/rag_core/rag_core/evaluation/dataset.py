"""EvalDataset JSONL loader (ADR-009 §7).

평가 케이스는 두 파일로 분리되어 운영된다:
  - qa.jsonl: case_id / question / 기대 답변 핵심 키워드 / ui_mode / config_override
  - citation_gold.jsonl: case_id / expected_chunk_ids[] (retrieval recall + citation accuracy 산정)

본 모듈은 두 파일을 case_id로 join 해 EvalDataset[EvalCase]를 만든다. platform/smoke.jsonl처럼
citation_gold가 없는 데이터셋은 expected_chunk_ids 빈 리스트로 처리된다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class EvalCase:
    """단일 평가 케이스."""

    case_id: str
    question: str
    domain_id: str
    expected_chunk_ids: list[str] = field(default_factory=list)
    must_include_keywords: list[str] = field(default_factory=list)
    must_not_include_keywords: list[str] = field(default_factory=list)
    expected_fallback: bool = False  # True면 본 케이스는 fallback이 정답
    ui_mode: str = "chat_structured"
    user_context: dict[str, Any] = field(default_factory=dict)
    config_override: dict[str, Any] = field(default_factory=dict)  # tenant_config 부분 override
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class EvalDataset:
    name: str
    domain_id: str
    cases: list[EvalCase]


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with path.open("r", encoding="utf-8") as fp:
        for line in fp:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            rows.append(json.loads(line))
    return rows


def load_dataset(
    *,
    name: str,
    domain_id: str,
    qa_path: Path,
    citation_gold_path: Path | None = None,
) -> EvalDataset:
    """qa.jsonl + citation_gold.jsonl을 case_id로 join 해 EvalDataset 빌드."""
    gold_by_case: dict[str, list[str]] = {}
    if citation_gold_path is not None and citation_gold_path.exists():
        for row in _read_jsonl(citation_gold_path):
            cid = row["case_id"]
            gold_by_case[cid] = list(row.get("expected_chunk_ids") or [])

    cases: list[EvalCase] = []
    for row in _read_jsonl(qa_path):
        case_id = row["case_id"]
        cases.append(
            EvalCase(
                case_id=case_id,
                question=row["question"],
                domain_id=row.get("domain_id", domain_id),
                expected_chunk_ids=gold_by_case.get(case_id, []),
                must_include_keywords=list(row.get("must_include_keywords") or []),
                must_not_include_keywords=list(row.get("must_not_include_keywords") or []),
                expected_fallback=bool(row.get("expected_fallback", False)),
                ui_mode=row.get("ui_mode", "chat_structured"),
                user_context=dict(row.get("user_context") or {}),
                config_override=dict(row.get("config_override") or {}),
                metadata=dict(row.get("metadata") or {}),
            )
        )
    return EvalDataset(name=name, domain_id=domain_id, cases=cases)


def load_platform_smoke(eval_root: Path) -> EvalDataset:
    """data/eval/platform/smoke.jsonl 로드. domain_id는 케이스 row에서 가져온다."""
    path = eval_root / "platform" / "smoke.jsonl"
    return load_dataset(
        name="platform_smoke",
        domain_id="platform",
        qa_path=path,
        citation_gold_path=None,
    )


def load_tenant_dataset(eval_root: Path, domain_id: str) -> EvalDataset:
    """data/eval/tenants/<domain_id>/{qa.jsonl, citation_gold.jsonl} 로드."""
    base = eval_root / "tenants" / domain_id
    return load_dataset(
        name=f"tenant_{domain_id}",
        domain_id=domain_id,
        qa_path=base / "qa.jsonl",
        citation_gold_path=base / "citation_gold.jsonl",
    )
