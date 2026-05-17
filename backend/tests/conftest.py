"""Backend tests — env 사전 설정 + auto mock auth injection.

ADR-018 §9 — MockAuthAdapter는 운영 코드에서 격리되고 본 conftest가 FastAPI
`app.dependency_overrides`로 주입한다 (2026-05-18 옵션 C).
"""

from __future__ import annotations

import os
import shutil
import tempfile
from pathlib import Path

import pytest

# Settings는 lru_cache이므로 import 전에 env 고정 필요.
# AUTH_MODE는 운영과 동일 "oidc" — conftest가 dep override로 mock adapter 주입하므로
# 실제 AuthFusion endpoint 호출 0건 (단위 테스트 환경에서 외부 의존 없음).
os.environ.setdefault("ENV", "development")
os.environ.setdefault("AUTH_MODE", "oidc")
os.environ.setdefault("RAG_BACKEND", "inmemory")
os.environ.setdefault(
    "CONFIG_DIR", str(Path(__file__).resolve().parents[2] / "configs")
)

# LocalSecretStore(KEYHUB_MODE=local)이 backend/var/secrets에 누적되지 않도록 세션
# 단위 tmpdir로 격리.
_keyhub_tmp = Path(tempfile.mkdtemp(prefix="domainrag_keyhub_"))
os.environ.setdefault("KEYHUB_LOCAL_PATH", str(_keyhub_tmp))


@pytest.fixture(scope="session", autouse=True)
def _cleanup_keyhub_tmp():
    yield
    shutil.rmtree(_keyhub_tmp, ignore_errors=True)


@pytest.fixture(autouse=True)
def _inject_mock_auth_adapter():
    """모든 테스트에서 AuthFusionAdapter 대신 MockAuthAdapter를 사용.

    JWT 검증·JWKS fetch·DB user_tenant_membership 조회 없이 default UserContext 반환.
    개별 테스트가 `app.dependency_overrides[get_user_context]=...`로 더 강한 override를
    걸면 그게 우선 (FastAPI dict 의미론). teardown에서 본 fixture 추가분만 제거.
    """
    from app.core.auth_adapter import get_auth_adapter
    from app.main import app
    from tests._fixtures.mock_auth import MockAuthAdapter

    mock_adapter = MockAuthAdapter()

    def _override():
        return mock_adapter

    app.dependency_overrides[get_auth_adapter] = _override
    try:
        yield
    finally:
        # 본 fixture가 주입한 mock adapter만 제거. 개별 테스트가 추가한 override는 그
        # 테스트의 teardown(또는 module-level fixture)이 책임진다.
        if app.dependency_overrides.get(get_auth_adapter) is _override:
            app.dependency_overrides.pop(get_auth_adapter, None)
