"""AssessmentFigureReuseGenerator — 그림 재사용 + 새 질문 생성 (ADR-025 §3b·§4)."""

from __future__ import annotations

import pytest

from rag_core.interfaces.assessment_item_repository import (
    AssessmentItemRecord,
    ExtractCriteria,
    InMemoryAssessmentItemRepository,
)
from rag_core.services.assessment_figure_reuse import (
    AssessmentFigureReuseGenerator,
    FigureReuseCriteria,
)


class _FakeVLM:
    def __init__(self, responses, healthy=True):
        self._responses = list(responses)
        self.healthy = healthy
        self.calls = []

    async def describe(self, *, image, prompt, model=None, max_tokens=1024, temperature=0.2):
        self.calls.append({"image_len": len(image), "model": model})
        return self._responses.pop(0) if self._responses else "{}"

    async def health(self):
        return self.healthy


def _loader(mapping):
    async def load(key):
        return mapping[key]  # KeyError → 호출측에서 skip 처리
    return load


def _fig_item(item_id, key="items/d/x/a.png", **kw):
    return AssessmentItemRecord(
        item_id=item_id, domain_id="t1", subject="정보보안", chapter="네트워크",
        difficulty="medium", question_type="multiple_choice",
        question_text=f"그림 문제 {item_id}", choices=["A", "B", "C", "D"], answer="A",
        quality_status=kw.pop("quality_status", "approved"),
        figure_dependent=kw.pop("figure_dependent", True),
        assets=kw.pop("assets", [{"asset_id": "a", "kind": "image", "storage_key": key}]),
        **kw,
    )


_VALID = (
    '{"question_text": "이 트리의 루트 노드는?", '
    '"choices": ["a", "b", "c", "d"], "answer": "a", "explanation": "루트는 a"}'
)


async def test_figure_reuse_generates_draft_reusing_asset():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_fig_item("REF-1", key="k1"))
    vlm = _FakeVLM([_VALID])
    gen = AssessmentFigureReuseGenerator(
        repository=repo, vlm=vlm, image_loader=_loader({"k1": b"\x89PNG_img"}),
    )
    res = await gen.generate(domain_id="t1", criteria=FigureReuseCriteria(subject="정보보안", count=1))

    assert res.generated_count == 1
    assert res.references_used == ["REF-1"]
    item = res.items[0]
    assert item.quality_status == "draft"          # Y2 — 자동 승인 없음
    assert item.source == "generated"
    assert item.figure_dependent is True
    assert item.reference_item_ids == ["REF-1"]
    assert item.assets[0]["storage_key"] == "k1"   # 같은 그림 승계 (신규 합성 아님)
    assert item.question_text == "이 트리의 루트 노드는?"
    assert item.answer == "a"
    assert vlm.calls[0]["image_len"] == len(b"\x89PNG_img")


async def test_figure_reuse_degrades_when_vlm_down():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_fig_item("REF-1", key="k1"))
    vlm = _FakeVLM([], healthy=False)
    gen = AssessmentFigureReuseGenerator(
        repository=repo, vlm=vlm, image_loader=_loader({"k1": b"x"}),
    )
    res = await gen.generate(domain_id="t1", criteria=FigureReuseCriteria(subject="정보보안"))
    assert res.vlm_unavailable is True
    assert res.generated_count == 0
    assert vlm.calls == []  # VLM 호출 안 함


async def test_figure_reuse_rejects_invalid_output():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_fig_item("REF-1", key="k1"))
    # 정답이 보기에 없음 → reject
    bad = '{"question_text": "q", "choices": ["a","b","c","d"], "answer": "zzz"}'
    vlm = _FakeVLM([bad])
    gen = AssessmentFigureReuseGenerator(
        repository=repo, vlm=vlm, image_loader=_loader({"k1": b"x"}),
    )
    res = await gen.generate(domain_id="t1", criteria=FigureReuseCriteria(subject="정보보안", count=1))
    assert res.generated_count == 0
    assert res.rejected_invalid == 1


async def test_figure_reuse_skips_missing_image():
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_fig_item("REF-1", key="missing-key"))
    vlm = _FakeVLM([_VALID])
    gen = AssessmentFigureReuseGenerator(
        repository=repo, vlm=vlm, image_loader=_loader({}),  # 빈 매핑 → KeyError
    )
    res = await gen.generate(domain_id="t1", criteria=FigureReuseCriteria(subject="정보보안", count=1))
    assert res.generated_count == 0
    assert res.skipped_no_image == 1


async def test_figure_reuse_ignores_non_figure_items():
    repo = InMemoryAssessmentItemRepository()
    # figure_dependent=False, 또는 assets 없음 → 후보 아님
    await repo.upsert(_fig_item("PLAIN", figure_dependent=False, assets=[]))
    vlm = _FakeVLM([_VALID])
    gen = AssessmentFigureReuseGenerator(
        repository=repo, vlm=vlm, image_loader=_loader({"k1": b"x"}),
    )
    res = await gen.generate(domain_id="t1", criteria=FigureReuseCriteria(subject="정보보안", count=1))
    assert res.generated_count == 0
    assert vlm.calls == []
