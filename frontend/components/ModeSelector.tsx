/**
 * ModeSelector — ADR-013 §6 + ADR-016 §6.
 * chat_structured (citation 검증 포함) vs chat_streaming (자유 대화) 전환.
 */
'use client';

interface Props {
  value: 'structured' | 'streaming';
  onChange: (v: 'structured' | 'streaming') => void;
}

export default function ModeSelector({ value, onChange }: Props) {
  return (
    <div className="inline-flex border rounded overflow-hidden text-sm">
      <button
        type="button"
        onClick={() => onChange('streaming')}
        className={`px-3 py-1 ${
          value === 'streaming'
            ? 'bg-blue-600 text-white'
            : 'bg-white text-gray-700 hover:bg-gray-50'
        }`}
        title="자유 대화 — RAG 검색 없이 LLM 직접 응답 (chat_streaming, ADR-013 §6)"
      >
        일반 대화
      </button>
      <button
        type="button"
        onClick={() => onChange('structured')}
        className={`px-3 py-1 ${
          value === 'structured'
            ? 'bg-blue-600 text-white'
            : 'bg-white text-gray-700 hover:bg-gray-50'
        }`}
        title="문서 검색 + 근거 인용 (chat_structured, ADR-010)"
      >
        문서 검색
      </button>
    </div>
  );
}
