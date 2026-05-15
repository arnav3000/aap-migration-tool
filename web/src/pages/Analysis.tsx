import { useState, useEffect, useCallback } from 'react';
import {
  Title,
  TextContent,
  Text,
  Button,
  Card,
  CardBody,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Alert,
  Split,
  SplitItem,
} from '@patternfly/react-core';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { Connection } from '../types/connection';

export function Analysis() {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [selectedConn, setSelectedConn] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  const loadConnections = useCallback(async () => {
    try {
      const conns = await api.listConnections() as Connection[];
      setConnections(conns.filter(c => c.role === 'source'));
    } catch { /* ignore */ }
  }, []);

  useEffect(() => { loadConnections(); }, [loadConnections]);

  const handleRun = async () => {
    if (!selectedConn) return;
    setError(null);
    setLoading(true);
    try {
      const res = await api.runAnalysis(selectedConn) as { job_id: string };
      navigate(`/jobs/${res.job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start analysis');
      setLoading(false);
    }
  };

  const sources = connections.filter(c => c.ping_status === 'ok' || c.ping_status === 'unknown');

  return (
    <>
      <Title headingLevel="h1" size="2xl">Dependency Analysis</Title>
      <TextContent style={{ marginBottom: 16 }}>
        <Text>Analyze cross-organization dependencies, migration ordering, and resource quality.</Text>
      </TextContent>

      <Card style={{ marginBottom: 16 }}>
        <CardBody>
          <Split hasGutter>
            <SplitItem isFilled>
              <FormGroup label="Source Connection" fieldId="conn">
                <FormSelect id="conn" value={selectedConn} onChange={(_e, v) => setSelectedConn(v)}>
                  <FormSelectOption value="" label="Select a source connection..." isDisabled />
                  {sources.map(c => (
                    <FormSelectOption key={c.id} value={c.id} label={`${c.name} (${c.url})`} />
                  ))}
                </FormSelect>
              </FormGroup>
            </SplitItem>
            <SplitItem style={{ alignSelf: 'flex-end' }}>
              <Button variant="primary" onClick={handleRun} isDisabled={!selectedConn || loading} isLoading={loading}>
                {loading ? 'Starting...' : 'Run Analysis'}
              </Button>
            </SplitItem>
          </Split>
        </CardBody>
      </Card>

      {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 16 }} />}
    </>
  );
}
