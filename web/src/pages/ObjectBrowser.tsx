import { useState, useEffect, useCallback } from 'react';
import {
  Title,
  TextContent,
  Text,
  FormSelect,
  FormSelectOption,
  FormGroup,
  Form,
  Alert,
  Spinner,
} from '@patternfly/react-core';
import { useSearchParams } from 'react-router-dom';
import { api } from '../api/client';
import { ResourceTable } from '../components/ResourceTable';
import type { Connection } from '../types/connection';
import type { ResourceType } from '../types/resources';

export function ObjectBrowser() {
  const [searchParams] = useSearchParams();
  const [connections, setConnections] = useState<Connection[]>([]);
  const [selectedConn, setSelectedConn] = useState(searchParams.get('conn') || '');
  const [resourceTypes, setResourceTypes] = useState<ResourceType[]>([]);
  const [selectedType, setSelectedType] = useState('');
  const [resources, setResources] = useState<Record<string, unknown>[]>([]);
  const [loading, setLoading] = useState(false);

  useEffect(() => {
    api.listConnections().then(c => setConnections(c as Connection[]));
  }, []);

  useEffect(() => {
    if (!selectedConn) return;
    api.listResourceTypes(selectedConn).then(rt => {
      const types = rt as ResourceType[];
      setResourceTypes(types);
      if (types.length > 0 && !selectedType) setSelectedType(types[0].name);
    });
  }, [selectedConn]);

  const loadResources = useCallback(async () => {
    if (!selectedConn || !selectedType) return;
    setLoading(true);
    try {
      const res = await api.listResources(selectedConn, selectedType);
      setResources((res || []) as Record<string, unknown>[]);
    } catch {
      setResources([]);
    }
    setLoading(false);
  }, [selectedConn, selectedType]);

  useEffect(() => { loadResources(); }, [loadResources]);

  return (
    <>
      <Title headingLevel="h1" size="2xl">Object Browser</Title>
      <Form isHorizontal onSubmit={(e) => e.preventDefault()} style={{ marginBottom: 16, maxWidth: 600, marginTop: 8 }}>
        <FormGroup label="Connection" fieldId="conn-select">
          <FormSelect id="conn-select" value={selectedConn} onChange={(_e, v) => { setSelectedConn(v); setSelectedType(''); setResources([]); setResourceTypes([]); }}>
            <FormSelectOption value="" label="-- Select connection --" isDisabled />
            {connections.map(c => (
              <FormSelectOption key={c.id} value={c.id} label={`${c.name} (${c.type.toUpperCase()})`} />
            ))}
          </FormSelect>
        </FormGroup>
        {resourceTypes.length > 0 && (
          <FormGroup label="Resource Type" fieldId="type-select">
            <FormSelect id="type-select" value={selectedType} onChange={(_e, v) => setSelectedType(v)}>
              {resourceTypes.map(rt => (
                <FormSelectOption key={rt.name} value={rt.name} label={rt.label} />
              ))}
            </FormSelect>
          </FormGroup>
        )}
      </Form>

      {loading && <Spinner size="lg" />}

      {!loading && resources.length > 0 && <ResourceTable resources={resources} />}

      {!loading && selectedConn && selectedType && resources.length === 0 && (
        <Alert variant="info" isInline title="No resources found for this type." />
      )}
    </>
  );
}
