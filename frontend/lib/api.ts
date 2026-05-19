/**
 * DomainRAG Ops — API client (ADR-017).
 *
 * 모든 endpoint를 함수 단위로 노출. SWR의 fetcher로 사용 가능.
 */

import type {
  AdapterRecord,
  AssessmentAnalytics,
  AssessmentExtractResult,
  AssessmentItem,
  AssessmentListResult,
  AssessmentQualityStatus,
  ChatLogListResult,
  ChatLogRow,
  ChatResponse,
  CitationDistributionResult,
  CitationReverifyResult,
  ConfigChangeRow,
  Conversation,
  ConversationDetail,
  ConversationListResult,
  DashboardSnapshot,
  DocumentDetail,
  DocumentListResult,
  DryrunResult,
  EndpointHealthRow,
  EvalDataset,
  EvalJob,
  EvalJobListResult,
  IndexingJob,
  IndexingJobListResult,
  InputSchemaHistory,
  InputSchemaRecord,
  InputTypeSchemaJson,
  LoRAStatus,
  PlatformUsageRow,
  PromptListResult,
  PromptPreviewResult,
  PromptRecord,
  ReindexMode,
  RoutingConfig,
  TenantListResult,
  TenantRow,
  TenantStatus,
  UiMode,
  UserContext,
} from './types';

export const API_BASE =
  process.env.NEXT_PUBLIC_API_URL ||
  process.env.NEXT_PUBLIC_API_BASE_URL ||
  'http://localhost:8001';

export class ApiError extends Error {
  status: number;
  detail: unknown;

  constructor(status: number, detail: unknown) {
    const msg =
      typeof detail === 'object' && detail !== null && 'error' in detail
        ? String((detail as Record<string, unknown>).error)
        : `HTTP ${status}`;
    super(msg);
    this.status = status;
    this.detail = detail;
  }
}

// ADR-018 §6 — Token refresh interceptor (httpOnly cookie only).
// 401 응답 시 단일 mutex로 refresh 1회 시도 + 원 요청 재시도. refresh 실패는
// /api/auth/authorize/{tenantId} redirect.
// XSS 노출면 최소화 — access_token을 localStorage에 두지 않는다. 모든 인증은
// httpOnly cookie(`domainrag_access`/`domainrag_refresh`)로만 이루어진다.
let _refreshInflight: Promise<boolean> | null = null;

async function _attemptRefresh(): Promise<boolean> {
  if (_refreshInflight) return _refreshInflight;
  _refreshInflight = (async () => {
    try {
      // tenant_id는 URL path에서 결정. SSR safe-guard.
      const tenantId =
        typeof window !== 'undefined'
          ? (window.location.pathname.split('/')[1] || '')
          : '';
      const res = await fetch(`${API_BASE}/api/auth/refresh`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ tenant_id: tenantId }),
      });
      return res.ok;
    } catch {
      return false;
    } finally {
      // 다음 401에 다시 시도 가능하도록 inflight 해제 (성공·실패 동일)
      setTimeout(() => {
        _refreshInflight = null;
      }, 0);
    }
  })();
  return _refreshInflight;
}

function _redirectToLogin(): void {
  if (typeof window === 'undefined') return;
  const pathname = window.location.pathname;
  // root('/'), /auth/* 는 redirect 트리거 skip — 무한 루프 방지.
  // 미인증 root 사용자는 페이지가 직접 로그인 링크를 노출하므로 자동 redirect 불필요.
  if (pathname === '/' || pathname.startsWith('/auth/')) return;
  const tenantId = pathname.split('/')[1] || '';
  // `?redirect=1`이면 backend가 IdP authorize URL로 302를 이어 준다 (ADR-018 §2).
  if (tenantId && tenantId !== 'platform') {
    window.location.href = `${API_BASE}/api/auth/authorize/${tenantId}?redirect=1`;
  }
  // tenantId가 빈 경우 또는 platform — 자동 redirect 안 함 (사용자가 명시적 navigation).
}

async function _fetchWithRefresh(
  url: string,
  init: RequestInit,
): Promise<Response> {
  const res = await fetch(url, init);
  if (res.status !== 401) return res;

  // 401 — token_expired 가정. refresh 시도 후 원 요청 재시도 1회만.
  // refresh 성공 시 새 httpOnly cookie가 set되어 있으므로 같은 init 그대로
  // 재요청. Authorization header는 사용 안 함.
  const refreshed = await _attemptRefresh();
  if (!refreshed) {
    _redirectToLogin();
    return res;
  }
  return fetch(url, init);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init?.headers as Record<string, string> | undefined),
  };
  // 인증은 httpOnly cookie로만 — Authorization header는 service-to-service
  // 호출이 명시적으로 init.headers에 넣을 때만 사용.
  const res = await _fetchWithRefresh(`${API_BASE}${path}`, {
    credentials: 'include',
    ...init,
    headers,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ error: 'request_failed' }));
    throw new ApiError(res.status, detail);
  }
  if (res.status === 204) {
    return undefined as T;
  }
  return res.json() as Promise<T>;
}

async function requestForm<T>(path: string, formData: FormData): Promise<T> {
  const headers: Record<string, string> = {};
  const auth = _authHeader();
  if (auth) headers.Authorization = auth;
  const res = await _fetchWithRefresh(`${API_BASE}${path}`, {
    method: 'POST',
    credentials: 'include',
    headers,
    body: formData,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({ error: 'request_failed' }));
    throw new ApiError(res.status, detail);
  }
  return res.json() as Promise<T>;
}

// SWR fetcher (URL → JSON)
export const swrFetcher = <T = unknown>(path: string) => request<T>(path);

// ============================================================================
// Chat / Conversation / Feedback (ADR-017 §3·§4·§5)
// ============================================================================

export async function chat(
  tenantId: string,
  body: {
    question: string;
    conversation_id?: string;
    ui_mode_request?: 'structured' | 'streaming';
  },
): Promise<ChatResponse> {
  return request<ChatResponse>(`/api/${tenantId}/chat`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function postFeedback(
  tenantId: string,
  body: { message_id: string; feedback: 'good' | 'bad'; comment?: string },
): Promise<void> {
  await request(`/api/${tenantId}/feedback`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function listConversations(
  tenantId: string,
  params: { page?: number; page_size?: number } = {},
): Promise<ConversationListResult> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  return request<ConversationListResult>(`/api/${tenantId}/conversations?${qs}`);
}

export async function getConversation(
  tenantId: string,
  conversationId: string,
): Promise<ConversationDetail> {
  return request(`/api/${tenantId}/conversations/${conversationId}`);
}

export async function updateConversationTitle(
  tenantId: string,
  conversationId: string,
  title: string,
): Promise<Conversation> {
  return request(`/api/${tenantId}/conversations/${conversationId}`, {
    method: 'PATCH',
    body: JSON.stringify({ title }),
  });
}

export async function deleteConversation(
  tenantId: string,
  conversationId: string,
): Promise<void> {
  await request(`/api/${tenantId}/conversations/${conversationId}`, {
    method: 'DELETE',
  });
}

// ============================================================================
// User (me)
// ============================================================================

export async function getCurrentUser(): Promise<UserContext> {
  // ADR-016 §3 Y9 — cross-tenant /api/auth/me 단일 진입점. tenant path mirror
  // 검증 없이 token만으로 roles 추출.
  return request<UserContext>(`/api/auth/me`);
}

// ============================================================================
// Chat Streaming (ADR-013 §6, ADR-017 §3.2) — SSE
// ============================================================================

export interface StreamingChatHandlers {
  onToken: (text: string) => void;
  onComplete: (payload: { message_id: string; metadata: Record<string, unknown> }) => void;
  onFallback?: (payload: Record<string, unknown>) => void;
  onError?: (payload: Record<string, unknown>) => void;
}

/**
 * `/api/{tid}/chat/stream` SSE. fetch + ReadableStream으로 cookie/credentials
 * 정상 전송. EventSource는 credentials 미지원이라 사용 안 함.
 */
export async function chatStream(
  tenantId: string,
  body: { question: string; conversation_id?: string },
  handlers: StreamingChatHandlers,
): Promise<void> {
  const res = await fetch(`${API_BASE}/api/${tenantId}/chat/stream`, {
    method: 'POST',
    credentials: 'include',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ ...body, ui_mode_request: 'streaming' }),
  });
  if (!res.ok || !res.body) {
    const detail = await res.json().catch(() => ({ error: 'stream_failed' }));
    throw new ApiError(res.status, detail);
  }
  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = '';
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    // SSE 메시지는 빈 줄("\n\n")으로 구분
    let sep: number;
    while ((sep = buffer.indexOf('\n\n')) >= 0) {
      const raw = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = 'message';
      const dataLines: string[] = [];
      for (const line of raw.split('\n')) {
        if (line.startsWith('event:')) event = line.slice(6).trim();
        else if (line.startsWith('data:')) dataLines.push(line.slice(5).trim());
      }
      const data = dataLines.join('\n');
      if (!data) continue;
      try {
        const parsed = JSON.parse(data);
        if (event === 'token') handlers.onToken(String(parsed.text ?? ''));
        else if (event === 'complete') handlers.onComplete(parsed);
        else if (event === 'fallback') handlers.onFallback?.(parsed);
        else if (event === 'error') handlers.onError?.(parsed);
      } catch {
        // ignore malformed SSE chunk
      }
    }
  }
}

export async function eraseMyChatLogs(
  tenantId: string,
  body: { mode: 'mask_only' | 'hard_delete'; reason: string },
): Promise<void> {
  await request(`/api/${tenantId}/me/chat_logs`, {
    method: 'DELETE',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Admin — Dashboard (ADR-017 §10)
// ============================================================================

export async function getDashboard(tenantId: string): Promise<DashboardSnapshot> {
  return request(`/api/${tenantId}/admin/dashboard`);
}

// ============================================================================
// Admin — Documents (ADR-017 §6)
// ============================================================================

export async function listDocuments(
  tenantId: string,
  params: {
    keyword?: string;
    approval_status?: string;
    page?: number;
    page_size?: number;
  } = {},
): Promise<DocumentListResult> {
  const qs = new URLSearchParams();
  if (params.keyword) qs.set('keyword', params.keyword);
  if (params.approval_status) qs.set('approval_status', params.approval_status);
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  return request(`/api/${tenantId}/admin/documents?${qs}`);
}

export async function getDocument(
  tenantId: string,
  docId: string,
  version = 'v1',
): Promise<DocumentDetail> {
  return request(`/api/${tenantId}/admin/documents/${docId}?version=${version}`);
}

export async function uploadDocument(
  tenantId: string,
  file: File,
  metadata: Record<string, unknown>,
  inputType?: string,
): Promise<{ job_id: string; doc_id: string; doc_version: string; status: string }> {
  const fd = new FormData();
  fd.append('file', file);
  fd.append('metadata', JSON.stringify(metadata));
  if (inputType) fd.append('input_type', inputType);
  return requestForm(`/api/${tenantId}/admin/documents/upload`, fd);
}

export async function reindexDocument(
  tenantId: string,
  docId: string,
  mode: ReindexMode,
  version = 'v1',
): Promise<{ job_id: string; doc_id: string; status: string }> {
  return request(`/api/${tenantId}/admin/documents/${docId}/reindex?version=${version}`, {
    method: 'POST',
    body: JSON.stringify({ mode }),
  });
}

export async function patchDocumentApproval(
  tenantId: string,
  docId: string,
  body: { status: 'pending' | 'approved' | 'archived'; reason?: string; version?: string },
): Promise<{ doc_id: string; affected_chunks: number }> {
  return request(`/api/${tenantId}/admin/documents/${docId}/approval`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function patchDocumentMetadata(
  tenantId: string,
  docId: string,
  body: { patch: Record<string, unknown>; version?: string; reason?: string },
): Promise<{ doc_id: string; affected_chunks: number; synced_keys: string[] }> {
  return request(`/api/${tenantId}/admin/documents/${docId}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function hardDeleteDocument(
  tenantId: string,
  docId: string,
  body: {
    reason: string;
    chat_logs_action?: 'keep_excerpts' | 'mask_excerpts' | 'delete_logs';
    version?: string;
  },
): Promise<{
  removed_chunks: number;
  removed_documents: number;
  storage_files: number;
  affected_chat_logs: number;
  dead_letters: unknown[];
}> {
  return request(`/api/${tenantId}/admin/documents/${docId}/hard`, {
    method: 'DELETE',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Admin — Indexing (ADR-017 §7)
// ============================================================================

export async function listIndexingJobs(
  tenantId: string,
  params: { page?: number; page_size?: number; status?: string } = {},
): Promise<IndexingJobListResult> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  if (params.status) qs.set('status', params.status);
  return request(`/api/${tenantId}/admin/indexing/jobs?${qs}`);
}

export async function getIndexingJob(
  tenantId: string,
  jobId: string,
): Promise<IndexingJob> {
  return request(`/api/${tenantId}/admin/indexing/jobs/${jobId}`);
}

export async function retryIndexingJob(
  tenantId: string,
  jobId: string,
): Promise<{ job_id: string; doc_id: string; status: string }> {
  return request(`/api/${tenantId}/admin/indexing/jobs/${jobId}/retry`, {
    method: 'POST',
  });
}

// ============================================================================
// Admin — Schema (ADR-017 §15)
// ============================================================================

export async function listInputSchemas(
  tenantId: string,
): Promise<{ items: Array<{ name: string; json_schema: InputTypeSchemaJson }> }> {
  return request(`/api/${tenantId}/admin/input_schemas`);
}

export async function getSchema(tenantId: string): Promise<InputSchemaRecord> {
  return request(`/api/${tenantId}/admin/schema`);
}

export async function putSchema(
  tenantId: string,
  body: {
    schema_yaml: Record<string, unknown>;
    ui_schema_yaml?: Record<string, unknown>;
    base_version?: string;
  },
): Promise<{ record: InputSchemaRecord; deprecated_version: string | null }> {
  return request(`/api/${tenantId}/admin/schema`, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
}

export async function getSchemaHistory(
  tenantId: string,
  params: { page?: number; page_size?: number } = {},
): Promise<InputSchemaHistory> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  return request(`/api/${tenantId}/admin/schema/history?${qs}`);
}

// ============================================================================
// Admin — Chat Logs (ADR-017 §8)
// ============================================================================

export interface ChatLogListFilters {
  user_id?: string;
  conversation_id?: string;
  from_date?: string;
  to_date?: string;
  fallback_only?: boolean;
  ui_mode?: UiMode;
  citation_type?: string;
  min_confidence?: number;
  max_confidence?: number;
  keyword?: string;
  page?: number;
  page_size?: number;
}

export async function listChatLogs(
  tenantId: string,
  filters: ChatLogListFilters = {},
): Promise<ChatLogListResult> {
  const qs = new URLSearchParams();
  Object.entries(filters).forEach(([k, v]) => {
    if (v !== undefined && v !== null && v !== '') qs.set(k, String(v));
  });
  return request(`/api/${tenantId}/admin/logs/chat?${qs}`);
}

export async function getChatLog(
  tenantId: string,
  requestId: string,
): Promise<ChatLogRow> {
  return request(`/api/${tenantId}/admin/logs/chat/${requestId}`);
}

// ============================================================================
// Admin — Citation Inspector (ADR-017 §9)
// ============================================================================

export async function getCitationDistribution(
  tenantId: string,
  params: { from_date?: string; to_date?: string; group_by?: 'day' | 'hour' } = {},
): Promise<CitationDistributionResult> {
  const qs = new URLSearchParams();
  if (params.from_date) qs.set('from_date', params.from_date);
  if (params.to_date) qs.set('to_date', params.to_date);
  if (params.group_by) qs.set('group_by', params.group_by);
  return request(`/api/${tenantId}/admin/citation-inspector/distribution?${qs}`);
}

export async function getCitationSegments(
  tenantId: string,
  messageId: string,
): Promise<{
  request_id: string;
  citations_by_type: Record<string, unknown[]>;
  verifier_metrics: Record<string, unknown>;
  inference_judge_results: unknown[];
  conflict_groups: unknown;
}> {
  return request(`/api/${tenantId}/admin/citation-inspector/segments/${messageId}`);
}

export async function reverifyCitations(
  tenantId: string,
  body: { from_date?: string; to_date?: string; max_records?: number },
): Promise<CitationReverifyResult> {
  return request(`/api/${tenantId}/admin/citation-inspector/reverify`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Admin — Routing (ADR-017 §13)
// ============================================================================

export async function getRouting(tenantId: string): Promise<RoutingConfig> {
  return request(`/api/${tenantId}/admin/routing`);
}

export async function putRouting(
  tenantId: string,
  value: RoutingConfig,
  reason?: string,
): Promise<{ tenant_id: string; routing: RoutingConfig }> {
  return request(`/api/${tenantId}/admin/routing`, {
    method: 'PUT',
    body: JSON.stringify({ value, reason }),
  });
}

export async function dryrunRouting(
  tenantId: string,
  body: {
    classifier_decision: Record<string, unknown>;
    sample_query?: string;
    routing_config?: RoutingConfig;
    retrieval_confidence?: number;
  },
): Promise<DryrunResult> {
  return request(`/api/${tenantId}/admin/routing/dryrun`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Admin — Prompts (ADR-017 §12)
// ============================================================================

export async function listPrompts(tenantId: string): Promise<PromptListResult> {
  return request(`/api/${tenantId}/admin/prompts`);
}

export async function getPrompt(
  tenantId: string,
  task: string,
  version?: string,
  abSlot?: string,
): Promise<PromptRecord> {
  const qs = new URLSearchParams();
  if (version) qs.set('version', version);
  if (abSlot) qs.set('ab_slot', abSlot);
  return request(`/api/${tenantId}/admin/prompts/${task}?${qs}`);
}

export async function patchPrompt(
  tenantId: string,
  task: string,
  version: string,
  abSlot: string,
  body: { system?: string; user?: string; reason?: string },
): Promise<PromptRecord> {
  return request(`/api/${tenantId}/admin/prompts/${task}/${version}/${abSlot}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function previewPrompt(
  tenantId: string,
  task: string,
  body: {
    system?: string;
    user?: string;
    sample_question: string;
    sample_contexts?: Array<Record<string, unknown>>;
    invoke_llm?: boolean;
  },
): Promise<PromptPreviewResult> {
  return request(`/api/${tenantId}/admin/prompts/${task}/preview`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Admin — LoRA Registry (ADR-017 §14)
// ============================================================================

export async function listLoRA(
  tenantId: string,
  status?: LoRAStatus,
): Promise<{ items: AdapterRecord[]; total: number }> {
  const qs = new URLSearchParams();
  if (status) qs.set('status', status);
  return request(`/api/${tenantId}/admin/lora?${qs}`);
}

export async function uploadLoRA(
  tenantId: string,
  file: File,
  metadata: {
    adapter_id: string;
    version?: string;
    base_model?: string;
    training_metadata?: Record<string, unknown>;
  },
): Promise<AdapterRecord> {
  const fd = new FormData();
  fd.append('weights', file);
  fd.append('metadata', JSON.stringify(metadata));
  return requestForm(`/api/${tenantId}/admin/lora/upload`, fd);
}

export async function activateLoRA(tenantId: string, adapterId: string): Promise<AdapterRecord> {
  return request(`/api/${tenantId}/admin/lora/${adapterId}/activate`, { method: 'POST' });
}

export async function retireLoRA(tenantId: string, adapterId: string): Promise<AdapterRecord> {
  return request(`/api/${tenantId}/admin/lora/${adapterId}/retire`, { method: 'POST' });
}

export async function deleteLoRA(tenantId: string, adapterId: string): Promise<void> {
  await request(`/api/${tenantId}/admin/lora/${adapterId}`, { method: 'DELETE' });
}

// ============================================================================
// Admin — Evaluation (ADR-017 §16)
// ============================================================================

export async function listEvalDatasets(
  tenantId: string,
): Promise<{ items: EvalDataset[] }> {
  return request(`/api/${tenantId}/admin/evaluation/datasets`);
}

export async function runEvalJob(
  tenantId: string,
  body: { dataset_name: string; config_override?: Record<string, unknown> },
): Promise<{ job_id: string; status: string }> {
  return request(`/api/${tenantId}/admin/evaluation/run`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function listEvalJobs(
  tenantId: string,
  params: { page?: number; page_size?: number } = {},
): Promise<EvalJobListResult> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  return request(`/api/${tenantId}/admin/evaluation/jobs?${qs}`);
}

export async function getEvalJob(tenantId: string, jobId: string): Promise<EvalJob> {
  return request(`/api/${tenantId}/admin/evaluation/jobs/${jobId}`);
}

export async function promoteEvalJob(
  tenantId: string,
  jobId: string,
  body: { target: string; version: string; reason?: string },
): Promise<EvalJob> {
  return request(`/api/${tenantId}/admin/evaluation/jobs/${jobId}/promote`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Admin — Configs (ADR-017 §11)
// ============================================================================

export async function getConfigs(tenantId: string): Promise<Record<string, unknown>> {
  return request(`/api/${tenantId}/admin/configs`);
}

export async function getConfigCategory(
  tenantId: string,
  category: string,
): Promise<Record<string, unknown>> {
  return request(`/api/${tenantId}/admin/configs/${category}`);
}

export async function patchConfig(
  tenantId: string,
  category: string,
  body: { key: string; value: unknown; reason?: string },
): Promise<{ category: string; key: string; old_value: unknown; new_value: unknown }> {
  return request(`/api/${tenantId}/admin/configs/${category}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function reloadConfig(tenantId: string): Promise<void> {
  await request(`/api/${tenantId}/admin/configs/reload`, { method: 'POST' });
}

export async function getConfigHistory(
  tenantId: string,
  params: { category: string; page?: number; page_size?: number },
): Promise<{ items: ConfigChangeRow[]; total: number; page: number; page_size: number }> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  const suffix = qs.toString() ? `?${qs}` : '';
  return request(
    `/api/${tenantId}/admin/configs/${encodeURIComponent(params.category)}/history${suffix}`,
  );
}

// ============================================================================
// Admin — Assessment (ADR-017 §17)
// ============================================================================

export async function listAssessmentItems(
  tenantId: string,
  params: {
    keyword?: string;
    subject?: string;
    difficulty?: string;
    quality_status?: AssessmentQualityStatus;
    page?: number;
    page_size?: number;
  } = {},
): Promise<AssessmentListResult> {
  const qs = new URLSearchParams();
  Object.entries(params).forEach(([k, v]) => {
    if (v !== undefined && v !== null) qs.set(k, String(v));
  });
  return request(`/api/${tenantId}/admin/assessment/items?${qs}`);
}

export async function createAssessmentItem(
  tenantId: string,
  body: Partial<AssessmentItem>,
): Promise<AssessmentItem> {
  return request(`/api/${tenantId}/admin/assessment/items`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function patchAssessmentItem(
  tenantId: string,
  itemId: string,
  body: Partial<AssessmentItem>,
): Promise<AssessmentItem> {
  return request(`/api/${tenantId}/admin/assessment/items/${itemId}`, {
    method: 'PATCH',
    body: JSON.stringify(body),
  });
}

export async function approveAssessmentItem(
  tenantId: string,
  itemId: string,
): Promise<AssessmentItem> {
  return request(`/api/${tenantId}/admin/assessment/items/${itemId}/approve`, {
    method: 'POST',
  });
}

export async function getReviewQueue(
  tenantId: string,
  params: { page?: number; page_size?: number } = {},
): Promise<AssessmentListResult> {
  const qs = new URLSearchParams();
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  return request(`/api/${tenantId}/admin/assessment/review-queue?${qs}`);
}

export async function getAssessmentAnalytics(
  tenantId: string,
): Promise<AssessmentAnalytics> {
  return request(`/api/${tenantId}/admin/assessment/analytics`);
}

export async function extractAssessment(
  tenantId: string,
  body: {
    subject?: string;
    chapter?: string;
    difficulty_distribution?: Record<string, number>;
    count: number;
    exclude_recent_days?: number;
    tags_any?: string[];
  },
): Promise<AssessmentExtractResult> {
  return request(`/api/${tenantId}/assessment/extract`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function generateAssessment(
  tenantId: string,
  body: { subject: string; chapter?: string; count: number; difficulty?: string },
): Promise<AssessmentExtractResult> {
  return request(`/api/${tenantId}/assessment/generate`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function hybridAssessment(
  tenantId: string,
  body: {
    extract: {
      subject?: string;
      chapter?: string;
      count: number;
      difficulty_distribution?: Record<string, number>;
      exclude_recent_days?: number;
      tags_any?: string[];
    };
    generate: {
      subject: string;
      chapter?: string;
      count: number;
      difficulty?: string;
    };
  },
): Promise<AssessmentExtractResult> {
  return request(`/api/${tenantId}/assessment/hybrid`, {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

// ============================================================================
// Platform Admin (ADR-017 §18)
// ============================================================================

export async function listTenants(
  params: { status?: TenantStatus; page?: number; page_size?: number } = {},
): Promise<TenantListResult> {
  const qs = new URLSearchParams();
  if (params.status) qs.set('status', params.status);
  if (params.page) qs.set('page', String(params.page));
  if (params.page_size) qs.set('page_size', String(params.page_size));
  return request(`/api/platform/admin/tenants?${qs}`);
}

export async function registerTenant(body: {
  tenant_id: string;
  display_name: string;
  domain_type: string;
  embedding_model?: string;
  modules?: string[];
}): Promise<TenantRow> {
  return request('/api/platform/admin/tenants', {
    method: 'POST',
    body: JSON.stringify(body),
  });
}

export async function getTenant(tenantId: string): Promise<TenantRow> {
  return request(`/api/platform/admin/tenants/${tenantId}`);
}

export async function patchTenantStatus(
  tenantId: string,
  status: TenantStatus,
  reason?: string,
): Promise<TenantRow> {
  return request(`/api/platform/admin/tenants/${tenantId}`, {
    method: 'PATCH',
    body: JSON.stringify({ status, reason }),
  });
}

export async function hardDeleteTenant(
  tenantId: string,
  reason: string,
): Promise<{ tenant_id: string; status: string; partial: boolean }> {
  return request(`/api/platform/admin/tenants/${tenantId}/hard`, {
    method: 'DELETE',
    body: JSON.stringify({ reason }),
  });
}

export async function listPlatformEndpoints(): Promise<{ items: EndpointHealthRow[] }> {
  return request('/api/platform/admin/endpoints');
}

export async function getPlatformUsage(): Promise<{ items: PlatformUsageRow[]; total: number }> {
  return request('/api/platform/admin/analytics/usage');
}

export async function getPlatformHealth(): Promise<{ items: EndpointHealthRow[] }> {
  return request('/api/platform/admin/analytics/health');
}

export async function getPlatformConfig(category: string): Promise<Record<string, unknown>> {
  return request(`/api/platform/admin/configs/${category}`);
}

export async function putPlatformConfig(
  category: string,
  body: { value: Record<string, unknown>; reason?: string },
): Promise<{ category: string }> {
  return request(`/api/platform/admin/configs/${category}`, {
    method: 'PUT',
    body: JSON.stringify(body),
  });
}

export async function getHealthMetrics(): Promise<{
  ledger: { publish_failures_total: number; dead_letter_count: number; recent_dead_letters: unknown[] };
  chat_log_writer: { write_failures_total: number; dead_letter_count: number; recent_dead_letters: unknown[] };
}> {
  return request('/api/platform/admin/health/metrics');
}
