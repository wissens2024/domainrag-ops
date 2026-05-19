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
        onClick={() => onChange('structured')}
        className={`px-3 py-1 ${
          value === 'structured'
            ? 'bg-blue-600 text-white'
            : 'bg-white text-gray-700 hover:bg-gray-50'
        }`}
        title="근거 인용 + 검증 (chat_structured, ADR-010)"
      >
        구조화
      </button>
      <button
        type="button"
        onClick={() => onChange('streaming')}
        className={`px-3 py-1 ${
          value === 'streaming'
            ? 'bg-blue-600 text-white'
            : 'bg-white text-gray-700 hover:bg-gray-50'
        }`}
        title="자유 대화 (chat_streaming, citation 비활성)"
      >
        스트리밍
      </button>
    </div>
  );
}
