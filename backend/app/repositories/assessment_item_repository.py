"""PostgresAssessmentItemRepository — ADR-014 assessment_items CRUD with RLS."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker

from rag_core.interfaces.assessment_item_repository import (
    AssessmentItemConflictError,
    AssessmentItemRecord,
    ExtractCriteria,
)

from app.core.rls import set_tenant_context


class PostgresAssessmentItemRepository:
    def __init__(self, *, session_factory: async_sessionmaker) -> None:
        self._sf = session_factory

    async def get(
        self, *, domain_id: str, item_id: str
    ) -> AssessmentItemRecord | None:
        async with self._sf() as session:
            await set_tenant_context(session, domain_id)
            row = (
                await session.execute(
                    text(
                        """
                        SELECT item_id, subject, chapter, difficulty, question_type,
                               question_text, choices, answer, explanation, tags,
                               quality_status, quality_score, validator_results,
                               used_count, last_used_at, source, reference_item_ids,
                               embedding_model, vector_id, created_at, updated_at,
                               assets, figure_dependent
                          FROM assessment_items
                         WHERE domain_id = :domain_id AND item_id = :item_id
                        """
                    ),
                    {"domain_id": domain_id, "item_id": item_id},
                )
            ).first()
        return _row_to_record(domain_id, row) if row else None

    async def upsert(
        self, record: AssessmentItemRecord
    ) -> AssessmentItemRecord:
        async with self._sf() as session:
            await set_tenant_context(session, record.domain_id)
            try:
                await session.execute(
                    text(
                        """
                        INSERT INTO assessment_items (
                            domain_id, item_id, subject, chapter, difficulty,
                            question_type, question_text, choices, answer,
                            explanation, tags, quality_status, quality_score,
                            validator_results, used_count, source,
                            reference_item_ids, embedding_model, vector_id,
                            assets, figure_dependent
                        )
                        VALUES (
                            :domain_id, :item_id, :subject, :chapter, :difficulty,
                            :question_type, :question_text,
                            CAST(:choices AS JSONB), :answer,
                            :explanation, CAST(:tags AS JSONB),
                            :quality_status, :quality_score,
                            CAST(:validator_results AS JSONB),
                            :used_count, :source,
                            CAST(:reference_item_ids AS JSONB),
                            :embedding_model, :vector_id,
                            CAST(:assets AS JSONB), :figure_dependent
                        )
                        ON CONFLICT (domain_id, item_id) DO UPDATE SET
                            subject = EXCLUDED.subject,
                            chapter = EXCLUDED.chapter,
                            difficulty = EXCLUDED.difficulty,
                            question_type = EXCLUDED.question_type,
                            question_text = EXCLUDED.question_text,
                            choices = EXCLUDED.choices,
                            answer = EXCLUDED.answer,
                            explanation = EXCLUDED.explanation,
                            tags = EXCLUDED.tags,
                            quality_status = EXCLUDED.quality_status,
                            quality_score = EXCLUDED.quality_score,
                            validator_results = EXCLUDED.validator_results,
                            source = EXCLUDED.source,
                            reference_item_ids = EXCLUDED.reference_item_ids,
                            embedding_model = EXCLUDED.embedding_model,
                            vector_id = EXCLUDED.vector_id,
                            assets = EXCLUDED.assets,
                            figure_dependent = EXCLUDED.figure_dependent,
                            updated_at = NOW()
                        """
                    ),
                    _record_to_params(record),
                )
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise AssessmentItemConflictError(record.item_id) from exc

        loaded = await self.get(
            domain_id=record.domain_id, item_id=record.item_id
        )
        assert loaded is not None
        return loaded

    async def list_by_tenant(
        self,
        *,
        domain_id: str,
        keyword: str | None = None,
        subject: str | None = None,
        chapter: str | None = None,
        difficulty: str | None = None,
        quality_status: str | None = None,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AssessmentItemRecord], int]:
        clauses = ["domain_id = :domain_id"]
        params: dict[str, Any] = {"domain_id": domain_id}
        if subject is not None:
            clauses.append("subject = :subject")
            params["subject"] = subject
        if chapter is not None:
            clauses.append("chapter = :chapter")
            params["chapter"] = chapter
        if difficulty is not None:
            clauses.append("difficulty = :difficulty")
            params["difficulty"] = difficulty
        if quality_status is not None:
            clauses.append("quality_status = :quality_status")
            params["quality_status"] = quality_status
        if keyword:
            clauses.append("(question_text ILIKE :kw OR item_id ILIKE :kw)")
            params["kw"] = f"%{keyword}%"
        where = " AND ".join(clauses)

        async with self._sf() as session:
            await set_tenant_context(session, domain_id)
            total = int(
                (
                    await session.execute(
                        text(f"SELECT COUNT(*) FROM assessment_items WHERE {where}"),
                        params,
                    )
                ).scalar()
                or 0
            )
            params2 = dict(params)
            params2["limit"] = page_size
            params2["offset"] = (page - 1) * page_size
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT item_id, subject, chapter, difficulty, question_type,
                               question_text, choices, answer, explanation, tags,
                               quality_status, quality_score, validator_results,
                               used_count, last_used_at, source, reference_item_ids,
                               embedding_model, vector_id, created_at, updated_at,
                               assets, figure_dependent
                          FROM assessment_items
                         WHERE {where}
                         ORDER BY updated_at DESC
                         LIMIT :limit OFFSET :offset
                        """
                    ),
                    params2,
                )
            ).all()
        items = [_row_to_record(domain_id, r) for r in rows]
        return items, total

    async def list_candidates_for_extract(
        self, *, domain_id: str, criteria: ExtractCriteria, limit: int = 200
    ) -> list[AssessmentItemRecord]:
        clauses = ["domain_id = :domain_id"]
        params: dict[str, Any] = {"domain_id": domain_id, "limit": limit}
        if criteria.subject is not None:
            clauses.append("subject = :subject")
            params["subject"] = criteria.subject
        if criteria.chapter is not None:
            clauses.append("chapter = :chapter")
            params["chapter"] = criteria.chapter
        if criteria.difficulty is not None:
            clauses.append("difficulty = :difficulty")
            params["difficulty"] = criteria.difficulty
        if criteria.quality_status:
            clauses.append("quality_status = ANY(:qs)")
            params["qs"] = list(criteria.quality_status)
        if criteria.exclude_recent_days is not None:
            threshold = datetime.utcnow() - timedelta(days=criteria.exclude_recent_days)
            clauses.append("(last_used_at IS NULL OR last_used_at < :threshold)")
            params["threshold"] = threshold
        if criteria.tags_any:
            clauses.append("tags ?| :tags")
            params["tags"] = list(criteria.tags_any)
        where = " AND ".join(clauses)

        async with self._sf() as session:
            await set_tenant_context(session, domain_id)
            rows = (
                await session.execute(
                    text(
                        f"""
                        SELECT item_id, subject, chapter, difficulty, question_type,
                               question_text, choices, answer, explanation, tags,
                               quality_status, quality_score, validator_results,
                               used_count, last_used_at, source, reference_item_ids,
                               embedding_model, vector_id, created_at, updated_at,
                               assets, figure_dependent
                          FROM assessment_items
                         WHERE {where}
                         LIMIT :limit
                        """
                    ),
                    params,
                )
            ).all()
        return [_row_to_record(domain_id, r) for r in rows]

    async def update_quality(
        self,
        *,
        domain_id: str,
        item_id: str,
        quality_status: str | None = None,
        quality_score: float | None = None,
        validator_results: dict[str, Any] | None = None,
    ) -> AssessmentItemRecord | None:
        set_clauses: list[str] = []
        params: dict[str, Any] = {"domain_id": domain_id, "item_id": item_id}
        if quality_status is not None:
            set_clauses.append("quality_status = :quality_status")
            params["quality_status"] = quality_status
        if quality_score is not None:
            set_clauses.append("quality_score = :quality_score")
            params["quality_score"] = quality_score
        if validator_results is not None:
            set_clauses.append("validator_results = CAST(:validator_results AS JSONB)")
            params["validator_results"] = json.dumps(
                validator_results, ensure_ascii=False, default=str,
            )
        if not set_clauses:
            return await self.get(domain_id=domain_id, item_id=item_id)
        set_clauses.append("updated_at = NOW()")
        async with self._sf() as session:
            await set_tenant_context(session, domain_id)
            result = await session.execute(
                text(
                    f"""
                    UPDATE assessment_items SET {', '.join(set_clauses)}
                     WHERE domain_id = :domain_id AND item_id = :item_id
                    """
                ),
                params,
            )
            await session.commit()
            if result.rowcount == 0:
                return None
        return await self.get(domain_id=domain_id, item_id=item_id)

    async def touch_used(
        self, *, domain_id: str, item_ids: list[str]
    ) -> int:
        if not item_ids:
            return 0
        async with self._sf() as session:
            await set_tenant_context(session, domain_id)
            result = await session.execute(
                text(
                    """
                    UPDATE assessment_items
                       SET used_count = used_count + 1, last_used_at = NOW()
                     WHERE domain_id = :domain_id
                       AND item_id = ANY(:item_ids)
                    """
                ),
                {"domain_id": domain_id, "item_ids": list(item_ids)},
            )
            await session.commit()
        return result.rowcount or 0

    async def bulk_approve(
        self,
        *,
        domain_id: str,
        subject: str | None = None,
        chapter: str | None = None,
        difficulty: str | None = None,
        keyword: str | None = None,
    ) -> int:
        clauses = [
            "domain_id = :domain_id",
            "quality_status IN ('draft', 'reviewed')",
        ]
        params: dict[str, Any] = {"domain_id": domain_id}
        if subject is not None:
            clauses.append("subject = :subject")
            params["subject"] = subject
        if chapter is not None:
            clauses.append("chapter = :chapter")
            params["chapter"] = chapter
        if difficulty is not None:
            clauses.append("difficulty = :difficulty")
            params["difficulty"] = difficulty
        if keyword:
            clauses.append("(question_text ILIKE :kw OR item_id ILIKE :kw)")
            params["kw"] = f"%{keyword}%"
        where = " AND ".join(clauses)
        async with self._sf() as session:
            await set_tenant_context(session, domain_id)
            result = await session.execute(
                text(
                    f"""
                    UPDATE assessment_items
                       SET quality_status = 'approved', updated_at = NOW()
                     WHERE {where}
                    """
                ),
                params,
            )
            await session.commit()
        return result.rowcount or 0

    async def list_review_queue(
        self,
        *,
        domain_id: str,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[AssessmentItemRecord], int]:
        async with self._sf() as session:
            await set_tenant_context(session, domain_id)
            total = int(
                (
                    await session.execute(
                        text(
                            """
                            SELECT COUNT(*) FROM assessment_items
                             WHERE domain_id = :domain_id
                               AND quality_status IN ('draft', 'reviewed')
                            """
                        ),
                        {"domain_id": domain_id},
                    )
                ).scalar()
                or 0
            )
            rows = (
                await session.execute(
                    text(
                        """
                        SELECT item_id, subject, chapter, difficulty, question_type,
                               question_text, choices, answer, explanation, tags,
                               quality_status, quality_score, validator_results,
                               used_count, last_used_at, source, reference_item_ids,
                               embedding_model, vector_id, created_at, updated_at,
                               assets, figure_dependent
                          FROM assessment_items
                         WHERE domain_id = :domain_id
                           AND quality_status IN ('draft', 'reviewed')
                         ORDER BY created_at ASC
                         LIMIT :limit OFFSET :offset
                        """
                    ),
                    {
                        "domain_id": domain_id,
                        "limit": page_size,
                        "offset": (page - 1) * page_size,
                    },
                )
            ).all()
        return [_row_to_record(domain_id, r) for r in rows], total

    async def analytics_summary(
        self, *, domain_id: str
    ) -> dict[str, Any]:
        async with self._sf() as session:
            await set_tenant_context(session, domain_id)
            total = int(
                (
                    await session.execute(
                        text(
                            "SELECT COUNT(*) FROM assessment_items WHERE domain_id = :t"
                        ),
                        {"t": domain_id},
                    )
                ).scalar()
                or 0
            )

            status_rows = (
                await session.execute(
                    text(
                        """
                        SELECT quality_status, COUNT(*) FROM assessment_items
                         WHERE domain_id = :t
                         GROUP BY quality_status
                        """
                    ),
                    {"t": domain_id},
                )
            ).all()
            diff_rows = (
                await session.execute(
                    text(
                        """
                        SELECT difficulty, COUNT(*) FROM assessment_items
                         WHERE domain_id = :t AND difficulty IS NOT NULL
                         GROUP BY difficulty
                        """
                    ),
                    {"t": domain_id},
                )
            ).all()
            subj_rows = (
                await session.execute(
                    text(
                        """
                        SELECT subject, COUNT(*) FROM assessment_items
                         WHERE domain_id = :t AND subject IS NOT NULL
                         GROUP BY subject
                        """
                    ),
                    {"t": domain_id},
                )
            ).all()
            unused = int(
                (
                    await session.execute(
                        text(
                            """
                            SELECT COUNT(*) FROM assessment_items
                             WHERE domain_id = :t AND used_count = 0
                            """
                        ),
                        {"t": domain_id},
                    )
                ).scalar()
                or 0
            )
        return {
            "total_items": total,
            "by_quality_status": {r[0]: int(r[1]) for r in status_rows},
            "by_difficulty": {r[0]: int(r[1]) for r in diff_rows},
            "by_subject": {r[0]: int(r[1]) for r in subj_rows},
            "unused_count": unused,
        }


def _record_to_params(rec: AssessmentItemRecord) -> dict[str, Any]:
    return {
        "domain_id": rec.domain_id,
        "item_id": rec.item_id,
        "subject": rec.subject,
        "chapter": rec.chapter,
        "difficulty": rec.difficulty,
        "question_type": rec.question_type,
        "question_text": rec.question_text,
        "choices": json.dumps(rec.choices or [], ensure_ascii=False, default=str),
        "answer": rec.answer,
        "explanation": rec.explanation,
        "tags": json.dumps(rec.tags or [], ensure_ascii=False, default=str),
        "quality_status": rec.quality_status,
        "quality_score": rec.quality_score,
        "validator_results": json.dumps(
            rec.validator_results or {}, ensure_ascii=False, default=str,
        ),
        "used_count": rec.used_count,
        "source": rec.source,
        "reference_item_ids": json.dumps(
            rec.reference_item_ids or [], ensure_ascii=False, default=str,
        ),
        "embedding_model": rec.embedding_model,
        "vector_id": rec.vector_id,
        "assets": json.dumps(rec.assets or [], ensure_ascii=False, default=str),
        "figure_dependent": bool(rec.figure_dependent),
    }


def _row_to_record(domain_id: str, row) -> AssessmentItemRecord:
    return AssessmentItemRecord(
        item_id=row[0],
        domain_id=domain_id,
        subject=row[1],
        chapter=row[2],
        difficulty=row[3],
        question_type=row[4],
        question_text=row[5] or "",
        choices=list(row[6] or []),
        answer=row[7] or "",
        explanation=row[8],
        tags=list(row[9] or []),
        quality_status=row[10],
        quality_score=row[11],
        validator_results=dict(row[12] or {}),
        used_count=int(row[13] or 0),
        last_used_at=row[14] if isinstance(row[14], datetime) else None,
        source=row[15],
        reference_item_ids=list(row[16] or []),
        embedding_model=row[17],
        vector_id=row[18],
        created_at=row[19] if isinstance(row[19], datetime) else None,
        updated_at=row[20] if isinstance(row[20], datetime) else None,
        assets=list(row[21] or []),
        figure_dependent=bool(row[22]),
    )
