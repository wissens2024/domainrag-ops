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

// ----------------------------------------------------------------------------
// Conversation (ADR-017 §4)
// ----------------------------------------------------------------------------

export interface Conversation {
  conversation_id: string;
  title: string | null;
  updated_at: string;
  message_count: number;
}

export interface ConversationListResult {
  items: Conversation[];
  total?: number;
  page: number;
  page_size: number;
}

export interface ConversationDetail {
  conversation_id: string;
  title: string | null;
  messages: Array<{
    role: 'user' | 'assistant';
    content: string;
    request_id?: string;
    created_at: string;
  }>;
}

// ----------------------------------------------------------------------------
// Document Management (ADR-017 §6 + ADR-012)
// ----------------------------------------------------------------------------

export type ApprovalStatus = 'pending' | 'approved' | 'archived';

export interface DocumentSummary {
  doc_id: string;
  version: string;
  title: string;
  input_type: string | null;
  source_type: string | null;
  approval_status: ApprovalStatus;
  department: string | null;
  doc_type: string | null;
  security_level: string | null;
  tags: string[];
  chunk_count: number;
  last_indexed_at: string | null;
  created_at: string;
}

export interface DocumentDetail extends DocumentSummary {
  object_storage_path: string;
  language: string | null;
  owner: string | null;
  valid_from: string | null;
  valid_until: string | null;
  metadata: Record<string, unknown>;
  chunks_summary?: {
    total: number;
    archived: number;
    failed: number;
  };
}

export interface DocumentListResult {
  items: DocumentSummary[];
  total?: number;
  page: number;
  page_size: number;
}

export type ReindexMode = 'full' | 'chunk_re_split' | 'embedding_only' | 'parser_only';

// ----------------------------------------------------------------------------
// Indexing Jobs (ADR-017 §7 + ADR-012)
// ----------------------------------------------------------------------------

export type IndexingStatus =
  | 'pending'
  | 'parsing'
  | 'chunking'
  | 'embedding'
  | 'indexing'
  | 'completed'
  | 'partial'
  | 'failed';

export interface IndexingJob {
  job_id: string;
  doc_id: string;
  doc_version: string;
  status: IndexingStatus;
  mode: ReindexMode | 'initial';
  progress: number;
  indexed_chunks: number;
  failed_chunks: Array<{ chunk_id: string; error: string }>;
  error_message: string | null;
  failure_rate: number | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface IndexingJobListResult {
  items: IndexingJob[];
  total?: number;
  page: number;
  page_size: number;
}

// ----------------------------------------------------------------------------
// Input Schema (ADR-015 + ADR-017 §15)
// ----------------------------------------------------------------------------

export interface InputTypeSchemaJson {
  $schema?: string;
  title?: string;
  type: 'object';
  required: string[];
  properties: Record<string, Record<string, unknown>>;
}

export interface InputSchemaRecord {
  schema_version: string;
  status: 'active' | 'deprecated';
  schema_yaml: Record<string, unknown>;
  ui_schema_yaml: Record<string, unknown>;
  created_at: string;
  deprecated_at: string | null;
}

export interface InputSchemaHistory {
  items: InputSchemaRecord[];
  total?: number;
  page: number;
  page_size: number;
}

// ----------------------------------------------------------------------------
// Chat Logs (ADR-017 §8)
// ----------------------------------------------------------------------------

export interface ChatLogRow {
  request_id: string;
  conversation_id: string | null;
  user_id: string | null;
  question: string | null;
  rewritten_query: string | null;
  answer: string | null;
  excerpt: unknown[];
  citations: Citation[];
  retrieved_chunks: unknown[];
  citation_types: SupportType[];
  confidence: number | null;
  ui_mode: UiMode;
  fallback_reason: string | null;
  routing_decision: Record<string, unknown> | null;
  classifier_decision: Record<string, unknown> | null;
  verifier_metrics: Record<string, unknown> | null;
  inference_judge_results: unknown[];
  conflict_groups: unknown;
  model_failure_chain: string[];
  input_pii_found: string[];
  output_pii_masked: string[];
  pii_storage_policy: string | null;
  feedback: 'good' | 'bad' | null;
  feedback_comment: string | null;
  latency_ms: number | null;
  created_at: string;
}

export interface ChatLogListResult {
  items: ChatLogRow[];
  total?: number;
  page: number;
  page_size: number;
}

// ----------------------------------------------------------------------------
// Citation Inspector (ADR-017 §9 + ADR-010)
// ----------------------------------------------------------------------------

export interface CitationDistributionBucket {
  bucket: string;
  counts: Record<SupportType, number>;
}

export interface CitationDistributionResult {
  granularity: 'day' | 'hour' | 'all';
  total_messages: number;
  buckets: CitationDistributionBucket[];
}

export interface CitationReverifyResult {
  scanned: number;
  updated: number;
  upgraded: number;
  downgraded: number;
  avg_similarity_before: number | null;
  avg_similarity_after: number | null;
}

// ----------------------------------------------------------------------------
// Routing (ADR-017 §13 + ADR-013)
// ----------------------------------------------------------------------------

export interface RoutingRule {
  name?: string;
  when?: {
    query_type?: string;
    support_type?: string;
    complexity?: string | string[];
    retrieval_confidence_below?: number;
  };
  use_model?: string;
  model?: string;
  action?: string;
  use_lora?: boolean;
  use_rag?: boolean;
  ui_mode?: UiMode;
  fallback_chain?: string[];
  citation_disabled?: boolean;
}

export interface RoutingConfig {
  default_route: RoutingRule;
  rules: RoutingRule[];
}

export interface DryrunResult {
  matched_rule: string | null;
  selected_model: string;
  selected_lora: string | null;
  fallback_chain_used: boolean;
  action?: string;
}

// ----------------------------------------------------------------------------
// Prompt Studio (ADR-017 §12)
// ----------------------------------------------------------------------------

export interface PromptRecord {
  task: string;
  version: string;
  ab_slot: string;
  system: string;
  user: string;
  schema_version: number | null;
  response_schema_path: string | null;
  source: 'platform' | 'tenant_runtime';
  updated_at: string | null;
  updated_by: string | null;
  reason: string | null;
}

export interface PromptListResult {
  items: PromptRecord[];
}

export interface PromptPreviewResult {
  rendered_system: string | null;
  rendered_user: string | null;
  render_error: string | null;
  sample_answer: string | null;
}

// ----------------------------------------------------------------------------
// LoRA Registry (ADR-017 §14 + ADR-013)
// ----------------------------------------------------------------------------

export type LoRAStatus = 'registered' | 'active' | 'retired';

export interface AdapterRecord {
  adapter_id: string;
  tenant_id: string;
  version: string | null;
  base_model: string | null;
  status: LoRAStatus;
  keyhub_secret_ref: string | null;
  training_metadata: Record<string, unknown>;
  created_at: string;
  activated_at: string | null;
  retired_at: string | null;
}

// ----------------------------------------------------------------------------
// Evaluation (ADR-017 §16 + ADR-009 §7)
// ----------------------------------------------------------------------------

export type EvalJobStatus = 'pending' | 'running' | 'completed' | 'failed' | 'promoted';

export interface EvalDataset {
  name: string;
  source: 'platform' | 'tenant';
  case_count: number;
  metric_count: number;
}

export interface EvalJob {
  job_id: string;
  dataset_name: string;
  status: EvalJobStatus;
  config_override: Record<string, unknown>;
  summary: Record<string, number> | null;
  gate_result: {
    passed: boolean;
    metrics: Record<string, { value: number; threshold: number; passed: boolean }>;
  } | null;
  promoted_at: string | null;
  promoted_by: string | null;
  promotion_target: string | null;
  promotion_version: string | null;
  started_at: string | null;
  finished_at: string | null;
  created_at: string;
}

export interface EvalJobListResult {
  items: EvalJob[];
  total?: number;
  page: number;
  page_size: number;
}

// ----------------------------------------------------------------------------
// Tenant Configs (ADR-017 §11 + ADR-009)
// ----------------------------------------------------------------------------

export interface ConfigChangeRow {
  category: string;
  path: string;
  old_value: unknown;
  new_value: unknown;
  author: string;
  reason: string | null;
  changed_at: string;
}

// ----------------------------------------------------------------------------
// Assessment (ADR-014 + ADR-017 §17)
// ----------------------------------------------------------------------------

export type AssessmentQualityStatus = 'draft' | 'reviewed' | 'approved' | 'retired';

export interface AssessmentItem {
  item_id: string;
  tenant_id: string;
  subject: string;
  chapter: string | null;
  difficulty: string | null;
  question_type: string;
  question_text: string;
  choices: string[];
  answer: string;
  explanation: string | null;
  tags: string[];
  quality_status: AssessmentQualityStatus;
  quality_score: number | null;
  used_count: number;
  last_used_at: string | null;
  generation_mode: 'manual' | 'extracted' | 'generated';
  source_item_ids: string[];
  reference_item_ids: string[];
  validator_results: Record<string, unknown>;
  created_at: string;
}

export interface AssessmentListResult {
  items: AssessmentItem[];
  total?: number;
  page: number;
  page_size: number;
}

export interface AssessmentExtractResult {
  items: AssessmentItem[];
  citations: Citation[];
  metadata: Record<string, unknown>;
}

export interface AssessmentAnalytics {
  total_items: number;
  by_status: Record<AssessmentQualityStatus, number>;
  by_subject: Record<string, number>;
  by_difficulty: Record<string, number>;
  recent_generations: number;
  approval_rate: number;
}

// ----------------------------------------------------------------------------
// Platform — Tenants (ADR-017 §18 + ADR-008)
// ----------------------------------------------------------------------------

export type TenantStatus = 'active' | 'suspended' | 'archived' | 'deleted';

export interface TenantRow {
  tenant_id: string;
  display_name: string;
  domain_type: string;
  embedding_model: string;
  status: TenantStatus;
  delete_status: string | null;
  modules: string[];
  deleted_at: string | null;
  created_at: string;
  updated_at: string;
}

export interface TenantListResult {
  items: TenantRow[];
  total?: number;
}

// ----------------------------------------------------------------------------
// Platform — Endpoints + Analytics (ADR-017 §18)
// ----------------------------------------------------------------------------

export interface EndpointHealthRow {
  name: string;
  url: string;
  backend: string;
  status: string;
}

export interface PlatformUsageRow {
  tenant_id: string;
  messages: number;
  fallbacks: number;
  avg_latency_ms: number;
}

// ----------------------------------------------------------------------------
// Dashboard (ADR-017 §10)
// ----------------------------------------------------------------------------

export interface DashboardSnapshot {
  tenant_id: string;
  total_documents: number;
  total_chunks: number;
  uploaded_today: number;
  indexing_completed_today: number;
  indexing_failed_today: number;
  questions_today: number;
  avg_latency_ms: number;
  answers_without_citation: number;
  negative_feedback_rate: number;
  citation_type_distribution: Record<SupportType, number>;
  fallback_distribution: Record<string, number>;
  routing_distribution: Record<string, number>;
}
