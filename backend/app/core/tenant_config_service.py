"""
TenantConfigService — platform defaults + tenant static + DB overrides 합성 (ADR-009).

caching:
  - tenant당 TenantConfig 객체 LRU + TTL 60초
  - PostgreSQL LISTEN tenant_config_changed 시 cache invalidate
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from threading import Lock
from time import time

import yaml

from app.core.config import get_settings


@dataclass
class TenantConfig:
    """런타임에서 사용되는 효과적 config 객체.

    실제 운영에서는 카테고리별 pydantic 모델로 strict 검증 (ADR-009 §4).
    골격 단계는 dict로.
    """

    tenant_id: str
    citation: dict = field(default_factory=dict)
    retrieval: dict = field(default_factory=dict)
    model: dict = field(default_factory=dict)
    routing: dict = field(default_factory=dict)
    query_classifier: dict = field(default_factory=dict)
    lifecycle: dict = field(default_factory=dict)
    auth: dict = field(default_factory=dict)
    pii: dict = field(default_factory=dict)
    audit: dict = field(default_factory=dict)
    data_retention: dict = field(default_factory=dict)
    compliance_mode: str = "standard"

    # 디버깅용 — 어느 source에서 왔는지 추적 (ADR-009 §5)
    _sources: dict = field(default_factory=dict)


class TenantConfigService:
    _cache: dict[str, tuple[TenantConfig, float]] = {}
    _lock = Lock()
    _ttl = 60.0
    # runtime overrides — ADR-017 §13 PUT routing (그리고 ADR-009 DB→load() 영구화 대기 동안)
    # tenant_id → {category: dict}. apply_runtime_override가 채우고 load()가 deep merge.
    _runtime_overrides: dict[str, dict[str, dict]] = {}

    @classmethod
    def load(cls, tenant_id: str) -> TenantConfig:
        with cls._lock:
            cached = cls._cache.get(tenant_id)
            if cached and (time() - cached[1] < cls._ttl):
                return cached[0]

        config = cls._load_from_disk(tenant_id)
        # runtime overrides 적용 (PUT routing 등) — 영구화는 ADR-009 DB→load() 작업
        runtime = cls._runtime_overrides.get(tenant_id) or {}
        for cat, value in runtime.items():
            if isinstance(value, dict) and hasattr(config, cat):
                current = getattr(config, cat) or {}
                setattr(config, cat, _deep_merge(current, value))

        with cls._lock:
            cls._cache[tenant_id] = (config, time())
        return config

    @classmethod
    def apply_runtime_override(
        cls, tenant_id: str, category: str, value: dict
    ) -> None:
        """ADR-017 §13 PUT — runtime layer에 카테고리 단위 dict override 저장.

        DB→load() 영구화가 끝나면 본 메서드는 단지 캐시 invalidate만 수행하도록
        간소화한다. 현재는 같은 프로세스 내 즉시 반영 보장.
        """
        cls._runtime_overrides.setdefault(tenant_id, {})[category] = value
        cls.invalidate(tenant_id)

    @classmethod
    def clear_runtime_overrides(cls, tenant_id: str | None = None) -> None:
        """테스트용 — runtime override 초기화."""
        if tenant_id is None:
            cls._runtime_overrides.clear()
        else:
            cls._runtime_overrides.pop(tenant_id, None)
        cls.invalidate(tenant_id)

    @classmethod
    def _load_from_disk(cls, tenant_id: str) -> TenantConfig:
        settings = get_settings()
        config_dir: Path = settings.config_dir.resolve()

        # 1) platform defaults
        platform_dir = config_dir / "platform"
        merged = {}
        for cat in ["citation", "retrieval", "model", "routing", "query_classifier",
                    "lifecycle", "auth", "pii", "audit", "data_retention"]:
            yaml_path = platform_dir / f"{cat}.yaml"
            if yaml_path.exists():
                merged[cat] = _read_yaml(yaml_path)
            else:
                merged[cat] = {}

        # 2) tenant static
        tenant_dir = config_dir / "tenants" / tenant_id
        if tenant_dir.exists():
            overrides_path = tenant_dir / "overrides.yaml"
            if overrides_path.exists():
                tenant_overrides = _read_yaml(overrides_path)
                merged = _deep_merge(merged, tenant_overrides)

        return TenantConfig(
            tenant_id=tenant_id,
            citation=merged.get("citation", {}),
            retrieval=merged.get("retrieval", {}),
            model=merged.get("model", {}),
            routing=merged.get("routing", {}),
            query_classifier=merged.get("query_classifier", {}),
            lifecycle=merged.get("lifecycle", {}),
            auth=merged.get("auth", {}),
            pii=merged.get("pii", {}),
            audit=merged.get("audit", {}),
            data_retention=merged.get("data_retention", {}),
            compliance_mode=merged.get("compliance_mode", "standard"),
        )

    @classmethod
    def invalidate(cls, tenant_id: str | None = None) -> None:
        """Cache invalidate. tenant_id 미지정 시 전체."""
        with cls._lock:
            if tenant_id is None:
                cls._cache.clear()
            else:
                cls._cache.pop(tenant_id, None)


def _read_yaml(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _deep_merge(base: dict, override: dict) -> dict:
    """깊은 병합 (ADR-009 §4 schema 진화 절차 정합).

    Y3 일관성: dict는 deep merge, list는 override가 base 교체, primitive는 override 우선.
    """
    if not isinstance(base, dict) or not isinstance(override, dict):
        return override
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out
