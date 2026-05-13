"""Backend tests — env 사전 설정 (mock auth + inmemory RAG)."""

from __future__ import annotations

import os
from pathlib import Path

# Settings는 lru_cache이므로 import 전에 env 고정 필요.
os.environ.setdefault("ENV", "development")
os.environ.setdefault("AUTH_MODE", "mock")
os.environ.setdefault("RAG_BACKEND", "inmemory")
os.environ.setdefault(
    "CONFIG_DIR", str(Path(__file__).resolve().parents[2] / "configs")
)
