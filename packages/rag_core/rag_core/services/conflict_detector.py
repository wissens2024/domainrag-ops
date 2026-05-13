"""ConflictDetector — ADR-010 §7 secondary 휴리스틱.

cited chunks 사이에 서로 다른 값(date / numeric / rule_id)이 발견되면 segment를
conflict 후보로 마킹한다. **Primary**(LLM이 직접 support_type=conflict 지정)는
이 detector를 거치지 않고 VerifierService Tier 2가 처리한다.

지원 패턴 (citation.yaml verification.conflict_detection.heuristic.patterns):
  - date_diff:    YYYY-MM-DD / YYYY.MM.DD / YYYY/MM/DD / YYYY년 MM월 DD일
  - numeric_diff: <숫자><단위> 의 숫자가 같은 단위에서 다른 경우
                  (단위: 일, 개월, 년, 자, 회, 원, 건, 점, 시간, 분, 명, 권)
  - rule_id_diff: 제N조(제N항)? / Article N / Sec(tion). N

NLI 기반은 Phase 2 (ADR-010 §7).
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

from ..interfaces.retriever import RetrievedChunk

DATE_PATTERNS = [
    # 한국어 텍스트(예: "2024-01-01입니다")에서도 매칭되도록 \b 대신 lookbehind/lookahead 숫자 경계.
    re.compile(r"(?<!\d)(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?!\d)"),
    re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일"),
]
_NUMERIC_UNITS = (
    "일", "개월", "년", "자", "회", "원", "건", "점",
    "시간", "분", "명", "권", "kg", "km", "%",
)
NUMERIC_UNIT_PATTERN = re.compile(
    r"(\d+(?:[.,]\d+)?)\s*(" + "|".join(re.escape(u) for u in _NUMERIC_UNITS) + r")"
)
RULE_ID_PATTERNS: list[tuple[re.Pattern, str]] = [
    # (pattern, label format) — group으로 일관된 canonical 표현 생성
    (re.compile(r"제\s*(\d+)\s*조\s*제\s*(\d+)\s*항"), "art-{0}-clause-{1}"),
    (re.compile(r"제\s*(\d+)\s*조(?!\s*제)"), "art-{0}"),
    (re.compile(r"Article\s+(\d+)", re.IGNORECASE), "art-{0}"),
    (re.compile(r"Sec(?:tion)?\.?\s*(\d+)", re.IGNORECASE), "art-{0}"),
]

ALLOWED_SIGNALS = ("date_diff", "numeric_diff", "rule_id_diff")


@dataclass
class ConflictDetectionResult:
    is_conflict: bool
    signal: str | None = None
    conflict_groups: list[list[int]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)


def _normalize_date(parts: tuple[str, str, str]) -> str:
    y, m, d = parts
    return f"{int(y):04d}-{int(m):02d}-{int(d):02d}"


def _extract_dates(text: str) -> set[str]:
    out: set[str] = set()
    for p in DATE_PATTERNS:
        for m in p.finditer(text):
            out.add(_normalize_date((m.group(1), m.group(2), m.group(3))))
    return out


def _extract_numeric_by_unit(text: str) -> dict[str, set[str]]:
    """{unit: {value, ...}} — 같은 단위에서 다른 값을 비교하기 위함."""
    out: dict[str, set[str]] = defaultdict(set)
    for m in NUMERIC_UNIT_PATTERN.finditer(text):
        value, unit = m.group(1), m.group(2)
        out[unit].add(value.replace(",", ""))
    return out


def _extract_rule_ids(text: str) -> set[str]:
    """canonical rule_id 집합. 표기(제5조 / Article 5 / Sec. 5)가 달라도 동일 art 번호면 같은 토큰."""
    out: set[str] = set()
    for pattern, label in RULE_ID_PATTERNS:
        for m in pattern.finditer(text):
            groups = [int(g) if g else 0 for g in m.groups()]
            out.add(label.format(*groups))
    return out


def _group_chunks_by_value(
    chunk_indices: list[int], values_per_chunk: dict[int, set[str]]
) -> list[list[int]]:
    """동일한 값 집합을 가진 chunk들을 한 그룹으로. 빈 set인 chunk는 제외."""
    groups: dict[frozenset[str], list[int]] = defaultdict(list)
    for cidx in chunk_indices:
        vals = values_per_chunk.get(cidx) or set()
        if not vals:
            continue
        groups[frozenset(vals)].append(cidx)
    return [sorted(g) for g in groups.values()]


class ConflictDetector:
    """ADR-010 §7 secondary 휴리스틱 감지기.

    enable=False / 비활성 패턴 / 단일 citation segment / LLM이 이미 conflict로
    마킹한 segment는 스킵한다.
    """

    def __init__(self, *, enabled_patterns: set[str]) -> None:
        # 알 수 없는 패턴 키 방어
        self._enabled = {p for p in enabled_patterns if p in ALLOWED_SIGNALS}

    @property
    def enabled(self) -> bool:
        return bool(self._enabled)

    @classmethod
    def from_config(cls, citation_config: dict | None) -> "ConflictDetector":
        verification = (citation_config or {}).get("verification") or {}
        cd = verification.get("conflict_detection") or {}
        h = cd.get("heuristic") or {}
        if not h.get("enable", False):
            return cls(enabled_patterns=set())
        patterns = set(h.get("patterns") or list(ALLOWED_SIGNALS))
        return cls(enabled_patterns=patterns)

    def detect_in_segment(
        self, segment: dict, contexts: list[RetrievedChunk]
    ) -> ConflictDetectionResult:
        """단일 segment의 cited chunks에서 휴리스틱 conflict 신호 1개 탐지.

        탐지된 첫 신호로 종료(early-return). conflict_groups는 발견된 값 별로 묶인다.
        """
        if not self._enabled:
            return ConflictDetectionResult(is_conflict=False)
        if segment.get("support_type") == "conflict":
            # Primary(LLM)가 이미 결정. heuristic은 관여하지 않는다.
            return ConflictDetectionResult(is_conflict=False)
        cidxs = [
            c for c in (segment.get("citations") or [])
            if 1 <= c <= len(contexts)
        ]
        if len(cidxs) < 2:
            return ConflictDetectionResult(is_conflict=False)

        # 패턴별 추출은 한 번만 (재사용)
        per_chunk_text = {c: contexts[c - 1].content or "" for c in cidxs}

        # date_diff
        if "date_diff" in self._enabled:
            dates = {c: _extract_dates(t) for c, t in per_chunk_text.items()}
            unique_dates = set().union(*dates.values()) if dates else set()
            if len(unique_dates) >= 2:
                groups = _group_chunks_by_value(cidxs, dates)
                if len(groups) >= 2:
                    return ConflictDetectionResult(
                        is_conflict=True,
                        signal="date_diff",
                        conflict_groups=groups,
                        details={
                            "values_per_chunk": {
                                str(c): sorted(v) for c, v in dates.items() if v
                            }
                        },
                    )

        # numeric_diff (unit 단위로 비교)
        if "numeric_diff" in self._enabled:
            per_chunk_units = {
                c: _extract_numeric_by_unit(t) for c, t in per_chunk_text.items()
            }
            # 단위 별로 chunk 간 값 차이가 있는지 확인 — 가장 먼저 발견되는 unit으로 그룹핑
            for unit in sorted({u for d in per_chunk_units.values() for u in d}):
                values_for_unit = {
                    c: per_chunk_units[c].get(unit, set()) for c in cidxs
                }
                contributing = {
                    c: v for c, v in values_for_unit.items() if v
                }
                if len(contributing) < 2:
                    continue
                unique_values: set[str] = set().union(*contributing.values())
                if len(unique_values) >= 2:
                    groups = _group_chunks_by_value(
                        list(contributing.keys()), values_for_unit
                    )
                    if len(groups) >= 2:
                        return ConflictDetectionResult(
                            is_conflict=True,
                            signal="numeric_diff",
                            conflict_groups=groups,
                            details={
                                "unit": unit,
                                "values_per_chunk": {
                                    str(c): sorted(v)
                                    for c, v in contributing.items()
                                },
                            },
                        )

        # rule_id_diff
        if "rule_id_diff" in self._enabled:
            ids = {c: _extract_rule_ids(t) for c, t in per_chunk_text.items()}
            unique_ids = set().union(*ids.values()) if ids else set()
            contributing = {c: v for c, v in ids.items() if v}
            if len(unique_ids) >= 2 and len(contributing) >= 2:
                groups = _group_chunks_by_value(list(contributing.keys()), ids)
                if len(groups) >= 2:
                    return ConflictDetectionResult(
                        is_conflict=True,
                        signal="rule_id_diff",
                        conflict_groups=groups,
                        details={
                            "values_per_chunk": {
                                str(c): sorted(v)
                                for c, v in contributing.items()
                            }
                        },
                    )

        return ConflictDetectionResult(is_conflict=False)
