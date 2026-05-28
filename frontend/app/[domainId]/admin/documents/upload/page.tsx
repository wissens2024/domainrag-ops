/**
 * Document Upload — /{tid}/admin/documents/upload (ADR-016 §3.2 + ADR-015 + ADR-017 §6.1).
 *
 * input_type 선택 → input_schema 동적 폼(react-jsonschema-form) → file → 업로드.
 */
'use client';

import Form from '@rjsf/core';
import type { RJSFSchema } from '@rjsf/utils';
import validator from '@rjsf/validator-ajv8';
import Link from 'next/link';
import { useRouter, useParams } from 'next/navigation';
import { useEffect, useState } from 'react';
import useSWR from 'swr';
import Card from '@/components/ui/Card';
import Button from '@/components/ui/Button';
import { listInputSchemas, uploadDocument } from '@/lib/api';
import type { InputTypeSchemaJson } from '@/lib/types';

interface SchemaListItem {
  name: string;
  json_schema: InputTypeSchemaJson;
}

export default function DocumentUploadPage() {
  const params = useParams<{ domainId: string }>();
  const domainId = params.domainId;
  const router = useRouter();

  const { data, error: schemaError } = useSWR<{ items: SchemaListItem[] }>(
    domainId ? `input_schemas:${domainId}` : null,
    () => listInputSchemas(domainId),
  );

  const [selectedType, setSelectedType] = useState<string>('');
  const [metadata, setMetadata] = useState<Record<string, unknown>>({});
  const [file, setFile] = useState<File | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (data?.items?.length && !selectedType) {
      setSelectedType(data.items[0].name);
    }
  }, [data, selectedType]);

  const currentSchema = data?.items.find((s) => s.name === selectedType)?.json_schema;

  const handleSubmit = async () => {
    if (!file) {
      setError('파일을 선택해주세요');
      return;
    }
    if (!selectedType) {
      setError('input_type을 선택해주세요');
      return;
    }
    setSubmitting(true);
    setError(null);
    try {
      const result = await uploadDocument(domainId, file, metadata, selectedType);
      alert(
        `업로드 완료\njob_id: ${result.job_id}\ndoc_id: ${result.doc_id}\n인덱싱 모니터링에서 진행 상황 확인 가능.`,
      );
      router.push(`/${domainId}/admin/indexing`);
    } catch (e) {
      if (e instanceof Error) {
        setError(e.message);
      } else {
        setError('업로드 실패');
      }
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <div className="p-6 max-w-3xl">
      <div className="flex items-center justify-between mb-4">
        <h1 className="text-2xl font-bold text-gray-900 dark:text-slate-100">문서 업로드</h1>
        <Link
          href={`/${domainId}/admin/documents`}
          className="text-sm text-blue-600 dark:text-brand-400 hover:underline"
        >
          ← 문서 목록
        </Link>
      </div>

      {schemaError && (
        <p className="text-red-600 dark:text-red-400 mb-3">
          input_schema 로드 실패: {schemaError.message}
        </p>
      )}

      <div className="mb-4">
        <label className="block text-sm font-bold mb-1 text-gray-900 dark:text-slate-100">input_type</label>
        <select
          value={selectedType}
          onChange={(e) => {
            setSelectedType(e.target.value);
            setMetadata({});
          }}
          className="w-full px-3 py-2 border rounded"
        >
          <option value="">선택해주세요</option>
          {data?.items.map((s) => (
            <option key={s.name} value={s.name}>
              {s.json_schema.title || s.name}
            </option>
          ))}
        </select>
        {data?.items.length === 0 && (
          <p className="text-xs text-gray-500 dark:text-slate-400 mt-1">
            input_type이 아직 정의되지 않았습니다. Schema Editor에서 먼저 정의하세요.
          </p>
        )}
      </div>

      {currentSchema && (
        <div className="mb-4">
          <label className="block text-sm font-bold mb-1 text-gray-900 dark:text-slate-100">메타데이터</label>
          <Card padded={false} className="p-3">
            <Form
              schema={currentSchema as RJSFSchema}
              validator={validator}
              formData={metadata}
              onChange={(e) => setMetadata(e.formData)}
              uiSchema={{ 'ui:submitButtonOptions': { norender: true } }}
            >
              <div />
            </Form>
          </Card>
        </div>
      )}

      <div className="mb-4">
        <label className="block text-sm font-bold mb-1 text-gray-900 dark:text-slate-100">파일</label>
        <input
          type="file"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="text-sm"
        />
        {file && (
          <p className="text-xs text-gray-500 dark:text-slate-400 mt-1">
            {file.name} ({(file.size / 1024).toFixed(1)} KB)
          </p>
        )}
      </div>

      {error && <p className="text-red-600 dark:text-red-400 text-sm mb-3">{error}</p>}

      <Button onClick={handleSubmit} disabled={submitting || !file || !selectedType}>
        {submitting ? '업로드 중...' : '업로드'}
      </Button>
    </div>
  );
}
