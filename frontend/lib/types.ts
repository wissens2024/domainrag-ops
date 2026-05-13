/**
 * API 응답 타입 (ADR-017 정합).
 * backend pydantic 모델과 일대일.
 */

// ----------------------------------------------------------------------------
// Citation (ADR-010)
// ----------------------------------------------------------------------------

export type SupportType = 'direct' | 'synthesis' | 'inference' | 'conflict';
export type SupportLevel = 'strong' | 'medium' | 'weak';

export interface Citation {
  citation_id: string;
  marker: string;
  tenant_id: string;
  support_type: SupportType;
  support_level: SupportLevel;
  verified: boolean;
  claim_text: string;
  doc_id: string;
  chunk_id: string;
  title: string;
  page_number: number | null;
  section_title: string | null;
  excerpt: string;
  score: number;
  rerank_score: number;
  // synthesis/inference/conflict의 추가 필드
  citations?: number[];
  inference_chain?: string;
  conflict_groups?: number[][];
  caveat?: string;
}

// ----------------------------------------------------------------------------
// Answer Segment (ADR-010 hybrid structured)
// ----------------------------------------------------------------------------

export interface AnswerSegment {
  text: string;
  citations: number[];
  support_type?: SupportType;
  unsupported?: boolean;
}

// ----------------------------------------------------------------------------
// Chat Response — success / fallback 분기 (ADR-017 §3)
// ----------------------------------------------------------------------------

export type UiMode = 'chat_structured' | 'chat_streaming';

export interface ChatMetadata {
  ui_mode: UiMode;
  llm_model: string;
  lora_adapter?: string;
  embedding_model?: string;
  reranker_model?: string;
  prompt_version?: string;
  latency_ms: number;
  confidence: number;
  verifier?: {
    tier1_markers_removed: number;
    tier2_avg_similarity: number;
    tier3_unsupported_segments: number;
    claim_extraction_mode: 'structured' | 'heuristic';
    inference_judge_results: unknown[];
  };
  routing_decision?: { matched_rule: string; fallback_chain_used: boolean };
  classifier_decision?: Record<string, unknown>;
}

export interface ChatSuccessResponse {
  status: 'success';
  conversation_id: string;
  message_id: string;
  answer: string;
  answer_segments: AnswerSegment[];
  citations: Citation[];
  metadata: ChatMetadata;
}

export interface NearMiss {
  doc_id: string;
  title: string;
  page_number: number | null;
  section_title: string | null;
  rerank_score: number;
}

export interface ChatFallbackResponse {
  status: 'fallback';
  conversation_id: string;
  message_id: string;
  answer: string;
  fallback: {
    reason:
      | 'low_retrieval'
      | 'low_generation_quality'
      | 'inference_judge_rejected'
      | 'conflict_unverifiable'
      | 'disallowed_type'
      | 'empty_tenant'
      | 'tenant_migrating'
      | 'retrieval_unavailable'
      | 'input_pii_blocked';
    near_misses: NearMiss[];
    suggested_actions: string[];
    retry_after_seconds?: number;
  };
  citations: [];
  metadata: ChatMetadata;
}

export type ChatResponse = ChatSuccessResponse | ChatFallbackResponse;

// ----------------------------------------------------------------------------
// User Context (ADR-018)
// ----------------------------------------------------------------------------

export interface UserContext {
  user_id: string;
  tenant_id: string;
  roles: string[];
  clearance: string;
  department: string | null;
  domain_groups: string[];
  preferred_username?: string;
  email?: string;
}
