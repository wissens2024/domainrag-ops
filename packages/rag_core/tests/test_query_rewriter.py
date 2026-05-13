"""QueryRewriter unit tests — ADR-011 §5 (HyDE / llm_expand)."""

from __future__ import annotations

from pathlib import Path

import pytest

from rag_core.clients.in_memory import InMemoryLLMClient
from rag_core.services.query_rewriter import (
    QueryRewriter,
    QueryRewritePrompt,
    RewriteResult,
)

REPO = Path(__file__).resolve().parents[3]
PROMPT_YAML = REPO / "configs" / "platform" / "prompts" / "query_rewrite.yaml"


def _build(
    *,
    tenant_response: str = "fake passage",
    shared_response: str | None = None,
    raise_error: bool = False,
) -> tuple[QueryRewriter, InMemoryLLMClient, InMemoryLLMClient]:
    if raise_error:
        class _BoomLLM(InMemoryLLMClient):
            async def generate(self, prompt, *, model, **kw):  # noqa: ARG002
                raise RuntimeError("vllm down")

        tenant = _BoomLLM(responses=[tenant_response])
    else:
        tenant = InMemoryLLMClient(responses=[tenant_response])
    shared = InMemoryLLMClient(
        responses=[shared_response if shared_response is not None else tenant_response]
    )
    rewriter = QueryRewriter(
        llm_clients={"tenant_slm": tenant, "shared_llm": shared},
        prompt=QueryRewritePrompt.load(PROMPT_YAML),
        default_model="qwen-7b",
    )
    return rewriter, tenant, shared


# --------------------------------------------------------------------------- #
# Disabled / none / unknown
# --------------------------------------------------------------------------- #


async def test_disabled_returns_original_question() -> None:
    rewriter, tenant, _ = _build()
    res = await rewriter.rewrite("패스워드 정책은?", {"enable": False})
    assert res.rewritten_query == "패스워드 정책은?"
    assert res.strategy == "none"
    assert res.error is None
    assert tenant.calls == []  # LLM 미호출


async def test_strategy_none_returns_original() -> None:
    rewriter, tenant, _ = _build()
    res = await rewriter.rewrite("Q", {"enable": True, "strategy": "none"})
    assert res.rewritten_query == "Q"
    assert res.strategy == "none"
    assert tenant.calls == []


async def test_unknown_strategy_falls_back_to_original() -> None:
    rewriter, tenant, _ = _build()
    res = await rewriter.rewrite(
        "Q", {"enable": True, "strategy": "magic_strategy"}
    )
    assert res.rewritten_query == "Q"
    assert res.error == "unknown_strategy:magic_strategy"
    assert tenant.calls == []


async def test_no_config_returns_original() -> None:
    rewriter, _, _ = _build()
    res = await rewriter.rewrite("Q", None)
    assert res.rewritten_query == "Q"
    assert res.strategy == "none"


# --------------------------------------------------------------------------- #
# HyDE
# --------------------------------------------------------------------------- #


async def test_hyde_uses_llm_passage_as_query() -> None:
    rewriter, tenant, _ = _build(tenant_response="가상의 답변 문단입니다.")
    res = await rewriter.rewrite(
        "패스워드 정책은?",
        {"enable": True, "strategy": "hyde"},
    )
    assert res.strategy == "hyde"
    assert res.rewritten_query == "가상의 답변 문단입니다."
    assert res.error is None
    assert len(tenant.calls) == 1
    # prompt에 system + 질문 포함
    assert "패스워드 정책은?" in tenant.calls[0]["prompt"]
    # default endpoint=tenant_slm 사용 (shared_llm 호출 없음)


async def test_hyde_uses_explicit_endpoint() -> None:
    rewriter, tenant, shared = _build(
        tenant_response="t-passage", shared_response="s-passage"
    )
    res = await rewriter.rewrite(
        "Q",
        {
            "enable": True,
            "strategy": "hyde",
            "llm": {"endpoint": "shared_llm"},
        },
    )
    assert res.rewritten_query == "s-passage"
    assert len(shared.calls) == 1
    assert tenant.calls == []


async def test_hyde_unknown_endpoint_graceful() -> None:
    rewriter, _, _ = _build()
    res = await rewriter.rewrite(
        "Q",
        {
            "enable": True,
            "strategy": "hyde",
            "llm": {"endpoint": "nonexistent"},
        },
    )
    assert res.rewritten_query == "Q"
    assert res.error == "endpoint_not_available:nonexistent"


async def test_hyde_empty_llm_output_falls_back() -> None:
    rewriter, _, _ = _build(tenant_response="   \n  ")
    res = await rewriter.rewrite("Q", {"enable": True, "strategy": "hyde"})
    assert res.rewritten_query == "Q"
    assert res.error == "empty_output"


async def test_hyde_llm_failure_falls_back_to_original() -> None:
    rewriter, _, _ = _build(raise_error=True)
    res = await rewriter.rewrite("Q", {"enable": True, "strategy": "hyde"})
    assert res.rewritten_query == "Q"
    assert res.error is not None and res.error.startswith("llm_error:")


# --------------------------------------------------------------------------- #
# llm_expand
# --------------------------------------------------------------------------- #


async def test_llm_expand_concatenates_keywords() -> None:
    rewriter, tenant, _ = _build(tenant_response="패스워드, 비밀번호, 만료")
    res = await rewriter.rewrite(
        "패스워드 정책",
        {"enable": True, "strategy": "llm_expand"},
    )
    assert res.strategy == "llm_expand"
    assert res.rewritten_query.startswith("패스워드 정책 ")
    # 추출된 키워드들이 포함됨
    for kw in ("패스워드", "비밀번호", "만료"):
        assert kw in res.rewritten_query


async def test_llm_expand_dedupes_and_caps_at_max_terms() -> None:
    rewriter, _, _ = _build(
        tenant_response="A, B, C, D, E, F, G, H, I, J, K"
    )
    res = await rewriter.rewrite(
        "Q",
        {
            "enable": True,
            "strategy": "llm_expand",
            "llm_expand": {"max_terms": 3},
        },
    )
    # max_terms=3 → 처음 3개만 들어감
    extras = res.rewritten_query.replace("Q ", "").split(" ")
    assert len(extras) == 3
    assert set(extras) <= {"A", "B", "C", "D", "E", "F", "G", "H", "I", "J", "K"}


async def test_llm_expand_empty_keywords_returns_original() -> None:
    rewriter, _, _ = _build(tenant_response="")
    res = await rewriter.rewrite(
        "Q", {"enable": True, "strategy": "llm_expand"}
    )
    assert res.rewritten_query == "Q"
    assert res.error == "empty_output"


async def test_llm_expand_filters_question_token_itself() -> None:
    """LLM이 질문 자체를 키워드로 반환해도 중복 제거된다."""
    rewriter, _, _ = _build(tenant_response="Q, foo, bar")
    res = await rewriter.rewrite(
        "Q", {"enable": True, "strategy": "llm_expand"}
    )
    # rewritten_query = "Q foo bar" (Q 자체는 추가되지 않음)
    parts = res.rewritten_query.split(" ")
    assert parts[0] == "Q"
    assert "foo" in parts and "bar" in parts


async def test_result_dataclass_default_safe() -> None:
    r = RewriteResult(rewritten_query="x", strategy="none")
    assert r.raw_output is None
    assert r.error is None
