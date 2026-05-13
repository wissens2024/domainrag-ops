"""QueryRewriter — ADR-011 §5 query rewriting (HyDE / llm_expand).

retrieve_context 직전에 사용자 질문을 보강해 dense+sparse retrieval recall을 높인다.
실패·timeout 시 graceful degradation으로 원 question을 그대로 통과시킨다 (검색이 막히지 않게).

전략:
  - hyde:       LLM이 질문에 답할 가상 문서 한 문단을 작성 → 그 본문을 query로 사용
  - llm_expand: LLM이 동의어·관련어 키워드를 추출 → 원 질문에 concat한 query 사용
  - none/disabled: 원 question 그대로 통과

LLM endpoint는 tenant_config.retrieval.query_rewriting.llm.endpoint('tenant_slm'|'shared_llm').
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Template

from ..interfaces.llm_client import LLMClient

ALLOWED_STRATEGIES = ("hyde", "llm_expand")


@dataclass
class RewriteResult:
    rewritten_query: str
    strategy: str            # hyde | llm_expand | none
    raw_output: str | None = None
    error: str | None = None


@dataclass
class QueryRewritePrompt:
    """configs/platform/prompts/query_rewrite.yaml 매핑.

    각 strategy(hyde/llm_expand)별 system+user 템플릿을 보유. user는 Jinja2.
    """

    hyde_system: str
    hyde_user: Template
    llm_expand_system: str
    llm_expand_user: Template

    @classmethod
    def load(cls, prompt_yaml: Path) -> "QueryRewritePrompt":
        with prompt_yaml.open("r", encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        hyde = data.get("hyde") or {}
        expand = data.get("llm_expand") or {}
        return cls(
            hyde_system=str(hyde.get("system", "")),
            hyde_user=Template(str(hyde.get("user", "{{ question }}"))),
            llm_expand_system=str(expand.get("system", "")),
            llm_expand_user=Template(str(expand.get("user", "{{ question }}"))),
        )


class QueryRewriter:
    """ADR-011 §5 query rewriter.

    Args:
        llm_clients: {"tenant_slm": LLMClient, "shared_llm": LLMClient} —
                     tenant_config.query_rewriting.llm.endpoint로 선택.
        prompt: QueryRewritePrompt (platform yaml).
        default_model: LLM 호출 시 model 파라미터 (예: 'qwen-7b').
    """

    def __init__(
        self,
        *,
        llm_clients: dict[str, LLMClient],
        prompt: QueryRewritePrompt,
        default_model: str,
    ) -> None:
        self._llms = dict(llm_clients)
        self._prompt = prompt
        self._default_model = default_model

    async def rewrite(
        self, question: str, config: dict[str, Any] | None
    ) -> RewriteResult:
        cfg = config or {}
        if not cfg.get("enable", False):
            return RewriteResult(rewritten_query=question, strategy="none")
        strategy = (cfg.get("strategy") or "none").lower()
        if strategy in {"none", "null", ""}:
            return RewriteResult(rewritten_query=question, strategy="none")
        if strategy not in ALLOWED_STRATEGIES:
            return RewriteResult(
                rewritten_query=question,
                strategy="none",
                error=f"unknown_strategy:{strategy}",
            )

        llm_cfg = cfg.get("llm") or {}
        endpoint = llm_cfg.get("endpoint", "tenant_slm")
        llm = self._llms.get(endpoint)
        if llm is None:
            return RewriteResult(
                rewritten_query=question,
                strategy=strategy,
                error=f"endpoint_not_available:{endpoint}",
            )
        model = llm_cfg.get("model") or self._default_model
        max_tokens = int(llm_cfg.get("max_tokens", 256))
        temperature = float(llm_cfg.get("temperature", 0.3))

        try:
            if strategy == "hyde":
                prompt = self._build_hyde_prompt(question)
            else:
                prompt = self._build_expand_prompt(question, cfg)
            raw = await llm.generate(
                prompt=prompt,
                model=model,
                max_tokens=max_tokens,
                temperature=temperature,
            )
        except Exception as e:  # noqa: BLE001 — graceful fallback
            return RewriteResult(
                rewritten_query=question,
                strategy=strategy,
                error=f"llm_error:{type(e).__name__}",
            )

        body = (raw or "").strip()
        if not body:
            return RewriteResult(
                rewritten_query=question,
                strategy=strategy,
                raw_output=raw,
                error="empty_output",
            )
        if strategy == "hyde":
            rewritten = body
        else:
            rewritten = self._merge_expand_keywords(question, body, cfg)
        return RewriteResult(
            rewritten_query=rewritten or question,
            strategy=strategy,
            raw_output=raw,
        )

    # ------------------------------------------------------------------ #

    def _build_hyde_prompt(self, question: str) -> str:
        sys = self._prompt.hyde_system
        usr = self._prompt.hyde_user.render(question=question)
        return f"{sys}\n\n{usr}".strip()

    def _build_expand_prompt(self, question: str, cfg: dict[str, Any]) -> str:
        max_terms = int((cfg.get("llm_expand") or {}).get("max_terms", 8))
        sys = self._prompt.llm_expand_system
        usr = self._prompt.llm_expand_user.render(
            question=question, max_terms=max_terms
        )
        sys = Template(sys).render(max_terms=max_terms)
        return f"{sys}\n\n{usr}".strip()

    @staticmethod
    def _merge_expand_keywords(
        question: str, raw_keywords: str, cfg: dict[str, Any]
    ) -> str:
        """LLM 출력 텍스트에서 keyword 토큰 추출 후 원 질문 뒤에 공백으로 concat."""
        max_terms = int((cfg.get("llm_expand") or {}).get("max_terms", 8))
        # 쉼표·세미콜론·줄바꿈으로 split. 기호 제거 후 dedupe + 빈 토큰 제거.
        rough: list[str] = []
        for chunk in raw_keywords.replace(";", ",").splitlines():
            for tok in chunk.split(","):
                tok = tok.strip(" ·-•\t")
                if tok and tok.lower() != question.lower():
                    rough.append(tok)
        seen: set[str] = set()
        out: list[str] = []
        for t in rough:
            key = t.lower()
            if key in seen:
                continue
            seen.add(key)
            out.append(t)
            if len(out) >= max_terms:
                break
        if not out:
            return question
        return question + " " + " ".join(out)
