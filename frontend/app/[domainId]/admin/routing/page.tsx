/**
 * Routing Rules — /{tid}/admin/routing (ADR-016 §3.6 + ADR-013 + ADR-017 §13).
 *
 * JSON editor + dry-run preview.
 */
'use client';

import dynamic from 'next/dynamic';
import { useParams } from 'next/navigation';
import { useTheme } from 'next-themes';
import { useEffect, useState } from 'react';
import useSWR from 'swr';
import { dryrunRouting, getRouting, putRouting } from '@/lib/api';
import type { DryrunResult, RoutingConfig } from '@/lib/types';

const Editor = dynamic(() => import('@monaco-editor/react').then((m) => m.default), {
  ssr: false,
  loading: () => <div className="p-4 text-gray-500">에디터 로딩...</div>,
});

export default function RoutingPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const { resolvedTheme } = useTheme();
  const editorTheme = resolvedTheme === 'dark' ? 'vs-dark' : 'light';
  const [yamlText, setYamlText] = useState('');
  const [message, setMessage] = useState<{ type: 'ok' | 'error'; text: string } | null>(null);
  const [dryrunQuery, setDryrunQuery] = useState('');
  const [dryrunType, setDryrunType] = useState('document_qa');
  const [dryrunComplexity, setDryrunComplexity] = useState('low');
  const [dryrunResult, setDryrunResult] = useState<DryrunResult | null>(null);

  const { data: current, mutate: refresh } = useSWR<RoutingConfig>(
    domainId ? `routing:${domainId}` : null,
    () => getRouting(domainId),
  );

  useEffect(() => {
    if (current) setYamlText(JSON.stringify(current, null, 2));
  }, [current]);

  const handleSave = async () => {
    setMessage(null);
    let parsed: RoutingConfig;
    try {
      parsed = JSON.parse(yamlText);
    } catch (e) {
      setMessage({ type: 'error', text: `JSON parse: ${e instanceof Error ? e.message : ''}` });
      return;
    }
    try {
      await putRouting(domainId, parsed);
      setMessage({ type: 'ok', text: '저장 완료. 같은 인스턴스 다음 chat에 즉시 반영.' });
      void refresh();
    } catch (e) {
      setMessage({ type: 'error', text: e instanceof Error ? e.message : 'save failed' });
    }
  };

  const handleDryrun = async () => {
    let routingConfig: RoutingConfig | undefined;
    try {
      routingConfig = JSON.parse(yamlText) as RoutingConfig;
    } catch {
      setMessage({ type: 'error', text: 'dry-run 전에 JSON 형식 정정 필요' });
      return;
    }
    try {
      const r = await dryrunRouting(domainId, {
        classifier_decision: {
          query_type: dryrunType,
          complexity: dryrunComplexity,
        },
        sample_query: dryrunQuery,
        routing_config: routingConfig,
      });
      setDryrunResult(r);
    } catch (e) {
      alert(`dry-run 실패: ${e instanceof Error ? e.message : ''}`);
    }
  };

  return (
    <div className="p-6 grid grid-cols-2 gap-6">
      <div>
        <h1 className="text-2xl font-bold mb-4">Routing Rules</h1>
        <div className="border rounded overflow-hidden mb-3" style={{ height: '500px' }}>
          <Editor
            language="json"
            theme={editorTheme}
            value={yamlText}
            onChange={(v) => setYamlText(v ?? '')}
            options={{ minimap: { enabled: false }, fontSize: 13, tabSize: 2 }}
          />
        </div>
        {message && (
          <div
            className={`mb-2 p-2 rounded text-sm ${
              message.type === 'ok' ? 'bg-green-50 text-green-800' : 'bg-red-50 text-red-800'
            }`}
          >
            {message.text}
          </div>
        )}
        <button
          onClick={handleSave}
          className="px-4 py-2 bg-blue-600 text-white rounded text-sm"
        >
          저장
        </button>
      </div>

      <div>
        <h2 className="text-xl font-bold mb-4">Dry-run</h2>
        <div className="space-y-2 text-sm">
          <div>
            <label className="block text-xs text-gray-500">sample_query</label>
            <input
              value={dryrunQuery}
              onChange={(e) => setDryrunQuery(e.target.value)}
              className="w-full px-2 py-1 border rounded"
              placeholder="(선택) 단순 표시용"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500">query_type</label>
            <select
              value={dryrunType}
              onChange={(e) => setDryrunType(e.target.value)}
              className="w-full px-2 py-1 border rounded"
            >
              <option value="document_qa">document_qa</option>
              <option value="synthesis">synthesis</option>
              <option value="inference">inference</option>
              <option value="meta">meta</option>
              <option value="free_chat">free_chat</option>
            </select>
          </div>
          <div>
            <label className="block text-xs text-gray-500">complexity</label>
            <select
              value={dryrunComplexity}
              onChange={(e) => setDryrunComplexity(e.target.value)}
              className="w-full px-2 py-1 border rounded"
            >
              <option value="low">low</option>
              <option value="medium">medium</option>
              <option value="high">high</option>
            </select>
          </div>
          <button
            onClick={handleDryrun}
            className="px-3 py-1 border rounded bg-blue-50"
          >
            ▶ Dry-run
          </button>
        </div>

        {dryrunResult && (
          <div className="mt-4 p-3 bg-gray-50 rounded text-sm">
            <p><b>matched_rule:</b> {dryrunResult.matched_rule || '(default)'}</p>
            <p><b>selected_model:</b> {dryrunResult.selected_model}</p>
            <p><b>selected_lora:</b> {dryrunResult.selected_lora || 'none'}</p>
            <p><b>fallback_chain_used:</b> {String(dryrunResult.fallback_chain_used)}</p>
            {dryrunResult.action && <p><b>action:</b> {dryrunResult.action}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
