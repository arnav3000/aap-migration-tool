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
  Checkbox,
  TextInput,
} from '@patternfly/react-core';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { Connection } from '../types/connection';

export function Validate() {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [sourceId, setSourceId] = useState('');
  const [destId, setDestId] = useState('');
  const [live, setLive] = useState(false);
  const [skipHosts, setSkipHosts] = useState(false);
  const [resourceType, setResourceType] = useState('');
  const [orgsRaw, setOrgsRaw] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const navigate = useNavigate();

  const loadConnections = useCallback(async () => {
    try {
      const conns = (await api.listConnections()) as Connection[];
      setConnections(conns);
    } catch {
      /* ignore */
    }
  }, []);

  useEffect(() => {
    loadConnections();
  }, [loadConnections]);

  const handleRun = async () => {
    if (live && !destId) {
      setError('Live mode requires a destination connection');
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const orgs = orgsRaw
        .split(',')
        .map(s => s.trim())
        .filter(Boolean);
      const res = (await api.runValidate({
        live,
        resource_type: resourceType || undefined,
        skip_hosts: skipHosts,
        organizations: orgs.length ? orgs : undefined,
        source_id: sourceId || undefined,
        destination_id: destId || undefined,
      })) as { job_id: string };
      navigate(`/jobs/${res.job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start validation');
      setLoading(false);
    }
  };

  const sourceConns = connections.filter(c => c.role === 'source');
  const destConns = connections.filter(c => c.role === 'destination' || c.role === 'target');

  return (
    <>
      <Title headingLevel="h1" size="2xl">
        Post-Migration Validation
      </Title>
      <TextContent style={{ marginBottom: 16 }}>
        <Text>
          Compare source exports against the migration database (default) or the live target API
          (--live). Same engine as <code>aap-bridge validate</code> — counts, existence, field
          parity, host sampling, and auditor cross-check.
        </Text>
      </TextContent>

      <Card style={{ marginBottom: 16 }}>
        <CardBody>
          <Split hasGutter style={{ marginBottom: 16 }}>
            <SplitItem isFilled>
              <FormGroup label="Source Connection (for report header)" fieldId="validate-source">
                <FormSelect
                  id="validate-source"
                  value={sourceId}
                  onChange={(_e, v) => setSourceId(v)}
                >
                  <FormSelectOption value="" label="Default (from server config)" />
                  {sourceConns.map(c => (
                    <FormSelectOption key={c.id} value={c.id} label={`${c.name} (${c.url})`} />
                  ))}
                </FormSelect>
              </FormGroup>
            </SplitItem>
            <SplitItem isFilled>
              <FormGroup label="Destination Connection (live mode)" fieldId="validate-dest">
                <FormSelect id="validate-dest" value={destId} onChange={(_e, v) => setDestId(v)}>
                  <FormSelectOption value="" label={live ? 'Select target...' : 'Not needed (DB mode)'} />
                  {destConns.map(c => (
                    <FormSelectOption key={c.id} value={c.id} label={`${c.name} (${c.url})`} />
                  ))}
                </FormSelect>
              </FormGroup>
            </SplitItem>
          </Split>

          <Split hasGutter style={{ marginBottom: 16, alignItems: 'flex-end' }}>
            <SplitItem>
              <Checkbox
                id="validate-live"
                label="Live — compare exports vs target API (identity match)"
                isChecked={live}
                onChange={(_e, v) => setLive(v)}
              />
            </SplitItem>
            <SplitItem>
              <Checkbox
                id="validate-skip-hosts"
                label="Skip hosts (T1–T4 host checks)"
                isChecked={skipHosts}
                onChange={(_e, v) => setSkipHosts(v)}
              />
            </SplitItem>
          </Split>

          <Split hasGutter style={{ marginBottom: 16 }}>
            <SplitItem isFilled>
              <FormGroup label="Resource type filter (--resource-type)" fieldId="validate-rtype">
                <TextInput
                  id="validate-rtype"
                  value={resourceType}
                  onChange={(_e, v) => setResourceType(v)}
                  placeholder="e.g. credentials (blank = all types)"
                />
              </FormGroup>
            </SplitItem>
            <SplitItem isFilled>
              <FormGroup label="Organizations (--orgs, comma-separated names)" fieldId="validate-orgs">
                <TextInput
                  id="validate-orgs"
                  value={orgsRaw}
                  onChange={(_e, v) => setOrgsRaw(v)}
                  placeholder="e.g. Team-alan, OrgB"
                />
              </FormGroup>
            </SplitItem>
          </Split>

          <Button
            variant="primary"
            onClick={handleRun}
            isDisabled={loading || (live && !destId)}
            isLoading={loading}
          >
            {loading ? 'Starting...' : live ? 'Run Live Validation' : 'Run DB Validation'}
          </Button>
          {skipHosts && resourceType === 'hosts' && (
            <Alert
              variant="warning"
              isInline
              title="--skip-hosts conflicts with resource_type=hosts"
              style={{ marginTop: 12 }}
            />
          )}
        </CardBody>
      </Card>

      {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 16 }} />}

      <Card isCompact>
        <CardBody>
          <TextContent>
            <Text component="small">
              Runs the same engine as CLI: <code>aap-bridge validate [--live] [--orgs ...] [-r ...] [--skip-hosts]</code>.
              Results include an HTML report (view via Job Detail → Download HTML) and JSON export.
              UI is optional — CLI remains the primary interface.
            </Text>
          </TextContent>
        </CardBody>
      </Card>
    </>
  );
}
