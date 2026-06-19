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
        self.calls.append({"image_len": len(image), "model": model, "prompt": prompt})
        return self._responses.pop(0) if self._responses else "{}"

    async def health(self):
        return self.healthy


class _FlakeVLM:
    """첫 N개 describe 호출은 예외(VLM 일시 오류), 이후 valid 응답."""

    def __init__(self, raise_first=1, valid=None):
        self._n = 0
        self._raise = raise_first
        self._valid = valid or _VALID
        self.healthy = True

    async def describe(self, **kw):
        self._n += 1
        if self._n <= self._raise:
            raise RuntimeError("VLM timeout (simulated)")
        return self._valid

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


async def test_figure_reuse_prompt_includes_original_question():
    """ADR-027 — 그림만이 아니라 원본 문제(질문·정답)도 VLM에 함께 전달한다."""
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(AssessmentItemRecord(
        item_id="REF-1", domain_id="t1", subject="정보보안", difficulty="medium",
        question_type="multiple_choice", question_text="다음 트리의 후위 순회 결과는?",
        choices=["a-b-d", "d-b-a", "a-d-b", "b-d-a"], answer="d-b-a",
        quality_status="approved", figure_dependent=True,
        assets=[{"asset_id": "a", "kind": "image", "storage_key": "k1"}]))
    vlm = _FakeVLM([_VALID])
    gen = AssessmentFigureReuseGenerator(
        repository=repo, vlm=vlm, image_loader=_loader({"k1": b"x"}))
    await gen.generate(domain_id="t1",
                       criteria=FigureReuseCriteria(subject="정보보안", count=1))
    prompt = vlm.calls[0]["prompt"]
    assert "다음 트리의 후위 순회 결과는?" in prompt  # 원본 문제 포함
    assert "d-b-a" in prompt                          # 원본 정답 포함


async def test_figure_reuse_resilient_to_per_ref_vlm_error():
    """ADR-027 근본수정 — 한 ref의 VLM 오류가 generate 전체를 abort시키지 않고
    다음 ref로 진행한다(이전엔 예외 전파→호출측이 '그림 없음'으로 오인)."""
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_fig_item("REF-1", key="k1"))
    await repo.upsert(_fig_item("REF-2", key="k2"))
    vlm = _FlakeVLM(raise_first=1)  # 첫 ref describe 실패, 둘째 성공
    gen = AssessmentFigureReuseGenerator(
        repository=repo, vlm=vlm, image_loader=_loader({"k1": b"x", "k2": b"y"}))
    res = await gen.generate(
        domain_id="t1", criteria=FigureReuseCriteria(subject="정보보안", count=1))
    assert res.generated_count == 1   # 한 ref 실패해도 다음 ref로 생성됨
    assert res.vlm_errors == 1        # VLM 오류 1건 기록(관측)


async def test_figure_reuse_rejects_duplicate_choices():
    """ADR-027 — 보기 4개가 모두 같으면(예: 전부 Gisapass) 폐기."""
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_fig_item("REF-1", key="k1"))
    dup = ('{"question_text": "그림의 글자는?", "choices": ["X","X","X","X"], '
           '"answer": "X", "explanation": "e"}')
    vlm = _FakeVLM([dup])
    gen = AssessmentFigureReuseGenerator(
        repository=repo, vlm=vlm, image_loader=_loader({"k1": b"x"}))
    res = await gen.generate(domain_id="t1",
                             criteria=FigureReuseCriteria(subject="정보보안", count=1))
    assert res.generated_count == 0
    assert res.rejected_invalid == 1


async def test_figure_reuse_rejects_watermark_question():
    """ADR-027 — 워터마크(Gisapass) 등 출처 표시를 묻는 문항 폐기."""
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_fig_item("REF-1", key="k1"))
    wm = ('{"question_text": "그림에서 Gisapass라는 단어가 몇 번 나오는가?", '
          '"choices": ["1번","2번","3번","4번"], "answer": "1번", "explanation": "e"}')
    vlm = _FakeVLM([wm])
    gen = AssessmentFigureReuseGenerator(
        repository=repo, vlm=vlm, image_loader=_loader({"k1": b"x"}))
    res = await gen.generate(domain_id="t1",
                             criteria=FigureReuseCriteria(subject="정보보안", count=1))
    assert res.generated_count == 0
    assert res.rejected_invalid == 1


async def test_figure_reuse_persist_false_does_not_upsert():
    """ADR-027 — 채팅 그림 출제(persist=False)는 DB에 저장하지 않는다."""
    repo = InMemoryAssessmentItemRepository()
    await repo.upsert(_fig_item("REF-1", key="k1"))
    vlm = _FakeVLM([_VALID])
    gen = AssessmentFigureReuseGenerator(
        repository=repo, vlm=vlm, image_loader=_loader({"k1": b"x"}))
    res = await gen.generate(domain_id="t1",
                             criteria=FigureReuseCriteria(subject="정보보안", count=1),
                             persist=False)
    assert res.generated_count == 1
    # DB엔 REF-1만 — 생성 문항(Q-*)은 저장 안 됨.
    rows, total = await repo.list_by_tenant(domain_id="t1")
    assert all(not r.item_id.startswith("Q-") for r in rows)
