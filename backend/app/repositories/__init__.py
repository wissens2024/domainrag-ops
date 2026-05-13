"""SQLAlchemy repository implementations (ADR-012).

rag_core.interfaces.chunk_repository에 정의된 Protocol을 만족하는 Postgres 구현.
세션 팩토리를 생성자에 주입받아 호출마다 새 session을 열고 set_tenant_context로
RLS context를 적용한다 — PostgresChatLogWriter와 동일한 패턴.
"""

from app.repositories.assessment_item_repository import (
    PostgresAssessmentItemRepository,
)
from app.repositories.chat_log_reader import PostgresChatLogReader
from app.repositories.chunk_repository import PostgresChunkRepository
from app.repositories.conversation_repository import PostgresConversationRepository
from app.repositories.document_repository import PostgresDocumentRepository
from app.repositories.evaluation_job_repository import PostgresEvaluationJobRepository
from app.repositories.indexing_job_repository import PostgresIndexingJobRepository
from app.repositories.lora_registry import PostgresLoRARegistry
from app.repositories.tenant_input_schema_repository import (
    PostgresTenantInputSchemaRepository,
)

__all__ = [
    "PostgresAssessmentItemRepository",
    "PostgresChatLogReader",
    "PostgresChunkRepository",
    "PostgresConversationRepository",
    "PostgresDocumentRepository",
    "PostgresEvaluationJobRepository",
    "PostgresIndexingJobRepository",
    "PostgresLoRARegistry",
    "PostgresTenantInputSchemaRepository",
]
