"""GenerationService — chat_structured 답변 생성 (ADR-010 §2 hybrid).

흐름:
  1. configs/platform/prompts/rag_answer.yaml + answer_schema.json 로드
  2. Jinja2로 system/user prompt 렌더 (question + contexts)
  3. LLMClient.generate(guided_json_schema=schema) 호출
  4. JSON 파싱 → answer_segments + limitations 반환
  5. 파싱 실패 시 fallback (raw_response 보존, segments 비움)
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml
from jinja2 import Environment, StrictUndefined

from ..interfaces.llm_client import LLMClient
from ..interfaces.retriever import RetrievedChunk

_jinja_env = Environment(undefined=StrictUndefined, autoescape=False)

# ADR-023 §3 — ungrounded(근거 미확보) 대화형 생성용 기본 system 지침.
# 도메인 사실(수치·조항·절차)을 단정하지 않도록 못 박는다. "근거 없음" 자체는
# UI 배지(ADR-023 §4)로 명시하므로 본문에 caveat를 강제하지 않는다.
_DEFAULT_CONVERSATIONAL_SYSTEM = (
    "당신은 도메인 지식 어시스턴트입니다. 다음 질문에 대해 등록된 문서에서 "
    "근거를 찾지 못했습니다. 일반 지식과 상식으로 간결하고 정중하게 답하되, "
    "도메인 특정 사실(수치·조항·절차·규정 등)을 확정적으로 단정하지 마세요. "
    "확실하지 않은 부분은 담당 부서 확인을 권하세요."
)


@dataclass
class GenerationResult:
    raw_response: str
    answer_segments: list[dict[str, Any]] = field(default_factory=list)
    limitations: str | None = None
    parse_ok: bool = True
    parse_error: str | None = None


@dataclass(frozen=True)
class GenerationPrompt:
    """rag_answer.yaml + answer_schema.json을 한 묶음으로 보유."""

    system: str
    user: str
    response_schema: dict[str, Any]

    @classmethod
    def load(cls, *, prompt_yaml: Path, schema_json: Path) -> "GenerationPrompt":
        with prompt_yaml.open("r", encoding="utf-8") as f:
            cfg = yaml.safe_load(f) or {}
        with schema_json.open("r", encoding="utf-8") as f:
            schema = json.load(f)
        return cls(
            system=str(cfg.get("system", "")),
            user=str(cfg.get("user", "")),
            response_schema=schema,
        )


def _context_view(chunks: list[RetrievedChunk]) -> list[dict]:
    return [
        {
            "title": c.title,
            "page_number": c.page_number,
            "section_title": c.section_title or "",
            "content": c.content,
        }
        for c in chunks
    ]


class GenerationService:
    """LLM 호출 + structured 응답 파싱.

    Args:
        llm: LLMClient 어댑터 (Vllm or InMemory mock)
        prompt: GenerationPrompt (system/user template + schema) — fallback
        model: LLM model 이름 (라우터 결정 결과)
        max_tokens: 응답 최대 토큰 (configs/platform/model.yaml defaults)
        temperature: 응답 temperature
        prompt_provider: 선택적 hook — Prompt Studio PATCH가 chat 흐름에 즉시 반영되도록
            매 generate_structured 호출 시 호출되어 effective prompt를 반환한다. None이면
            생성자에 주입된 self._prompt 사용 (현 ADR-017 §12 follow-up 한계 해소).
    """

    def __init__(
        self,
        *,
        llm: LLMClient,
        prompt: GenerationPrompt,
        model: str,
        max_tokens: int = 1024,
        temperature: float = 0.2,
        prompt_provider=None,
        conversational_system: str | None = None,
    ) -> None:
        self._llm = llm
        self._prompt = prompt
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._prompt_provider = prompt_provider
        self._conversational_system = (
            conversational_system or _DEFAULT_CONVERSATIONAL_SYSTEM
        )

    def _effective_prompt(self, domain_id: str | None = None) -> GenerationPrompt:
        if self._prompt_provider is not None:
            try:
                p = self._prompt_provider(domain_id)
                if p is not None:
                    return p
            except Exception:  # noqa: BLE001 — provider 실패 시 fallback
                pass
        return self._prompt

    def _render(
        self, *, question: str, contexts: list[RetrievedChunk],
        domain_id: str | None = None,
    ) -> str:
        prompt = self._effective_prompt(domain_id)
        system = _jinja_env.from_string(prompt.system).render()
        user = _jinja_env.from_string(prompt.user).render(
            question=question, contexts=_context_view(contexts)
        )
        # vLLM chat completion은 message 단위지만 LLMClient.generate는 단일 prompt 시그니처.
        # 단일 prompt에 system+user를 plain join — 운영에서 message-level이 필요해지면
        # LLMClient.generate에 messages 인자를 추가하는 ADR amendment.
        return f"{system}\n\n{user}"

    async def generate_structured(
        self,
        *,
        question: str,
        contexts: list[RetrievedChunk],
        lora_adapter: str | None = None,
        domain_id: str | None = None,
        model_override: str | None = None,
    ) -> GenerationResult:
        effective = self._effective_prompt(domain_id)
        rendered = self._render(
            question=question, contexts=contexts, domain_id=domain_id,
        )
        raw = await self._llm.generate(
            rendered,
            model=model_override or self._model,
            max_tokens=self._max_tokens,
            temperature=self._temperature,
            guided_json_schema=effective.response_schema,
            lora_adapter=lora_adapter,
        )
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as e:
            return GenerationResult(
                raw_response=raw,
                answer_segments=[],
                limitations=None,
                parse_ok=False,
                parse_error=f"json_decode_error: {e}",
            )
        segments = parsed.get("answer_segments") or []
        if not isinstance(segments, list):
            return GenerationResult(
                raw_response=raw,
                answer_segments=[],
                limitations=None,
                parse_ok=False,
                parse_error="answer_segments not a list",
            )
        return GenerationResult(
            raw_response=raw,
            answer_segments=segments,
            limitations=parsed.get("limitations"),
            parse_ok=True,
        )

    async def generate_conversational(
        self,
        *,
        question: str,
        lora_adapter: str | None = None,
        domain_id: str | None = None,
        model_override: str | None = None,
    ) -> str:
        """ADR-023 §3 — ungrounded 경로 자유 텍스트 생성.

        guided_json_schema 없이 LLM을 호출해 대화형 답변(plain text)을 반환한다.
        citable context를 주입하지 않아 인용 마커가 새지 않는다. "근거 없음"은
        호출자가 grounding="ungrounded"로 표시하고 UI 배지로 구분한다(ADR-023 §4).
        """
        prompt = f"{self._conversational_system}\n\n{question}"
        return await self._llm.generate(
            prompt,
            model=model_override or self._model,
            max_tokens=self._max_tokens,
            temperature=max(self._temperature, 0.5),
            lora_adapter=lora_adapter,
        )
