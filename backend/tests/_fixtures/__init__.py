"""Test-only fixtures — 운영 코드에서 import 금지.

본 모듈에 들어있는 객체(MockAuthAdapter 등)는 *테스트 환경에서만* 사용되며
운영 backend가 import하면 보안 사고. ADR-018 §9 (mock은 test-only).
"""
