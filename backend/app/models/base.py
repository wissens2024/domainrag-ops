"""SQLAlchemy 2.0 Declarative Base — 모든 ORM 모델의 부모."""

from __future__ import annotations

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """domainrag database 전역 Base."""
