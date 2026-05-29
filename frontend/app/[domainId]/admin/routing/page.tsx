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
import Card from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import { dryrunRouting, getRouting, putRouting } from '@/lib/api';
import type { DryrunResult, RoutingConfig } from '@/lib/types';

const Editor = dynamic(() => import('@monaco-editor/react').then((m) => m.default), {
  ssr: false,
  loading: () => <div className="p-4 text-gray-500 dark:text-slate-400">에디터 로딩...</div>,
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
        <h1 className="text-2xl font-bold mb-4 text-gray-900 dark:text-slate-100">Routing Rules</h1>
        <div className="border border-gray-200 dark:border-slate-700 rounded overflow-hidden mb-3" style={{ height: '500px' }}>
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
              message.type === 'ok'
                ? 'bg-green-50 text-green-800 dark:bg-green-900/30 dark:text-green-300'
                : 'bg-red-50 text-red-800 dark:bg-red-900/30 dark:text-red-300'
            }`}
          >
            {message.text}
          </div>
        )}
        <Button onClick={handleSave}>
          저장
        </Button>
      </div>

      <div>
        <h2 className="text-xl font-bold mb-4 text-gray-900 dark:text-slate-100">Dry-run</h2>
        <div className="space-y-2 text-sm">
          <div>
            <label className="block text-xs text-gray-500 dark:text-slate-400">sample_query</label>
            <input
              value={dryrunQuery}
              onChange={(e) => setDryrunQuery(e.target.value)}
              className="w-full px-2 py-1 border rounded"
              placeholder="(선택) 단순 표시용"
            />
          </div>
          <div>
            <label className="block text-xs text-gray-500 dark:text-slate-400">query_type</label>
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
            <label className="block text-xs text-gray-500 dark:text-slate-400">complexity</label>
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
          <Button variant="secondary" size="sm" onClick={handleDryrun}>
            ▶ Dry-run
          </Button>
        </div>

        {dryrunResult?.decision && (
          <div className="mt-4 p-3 bg-gray-50 dark:bg-slate-900/50 rounded text-sm text-gray-700 dark:text-slate-300">
            <p><b>matched_rule:</b> {dryrunResult.decision.matched_rule || '(default)'}</p>
            <p><b>model:</b> {dryrunResult.decision.model}</p>
            <p><b>lora_adapter:</b> {dryrunResult.decision.lora_adapter || 'none'}</p>
            <p><b>ui_mode:</b> {dryrunResult.decision.ui_mode}</p>
            <p><b>use_rag:</b> {String(dryrunResult.decision.use_rag)}</p>
            {dryrunResult.decision.action && <p><b>action:</b> {dryrunResult.decision.action}</p>}
          </div>
        )}
      </div>
    </div>
  );
}
