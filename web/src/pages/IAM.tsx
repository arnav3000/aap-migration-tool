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
  NumberInput,
  Tabs,
  Tab,
  TabTitleText,
} from '@patternfly/react-core';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { Connection } from '../types/connection';

export function IAM() {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [sourceId, setSourceId] = useState('');
  const [destId, setDestId] = useState('');
  const [activeTab, setActiveTab] = useState<string>('audit');
  const [workers, setWorkers] = useState(1);
  const [scanStrategy, setScanStrategy] = useState<'resource' | 'principal'>('resource');
  const [dryRun, setDryRun] = useState(false);
  const [skipUserRoles, setSkipUserRoles] = useState(false);
  const [usersOnly, setUsersOnly] = useState(false);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [benchOutput, setBenchOutput] = useState<string | null>(null);
  const [benchLoading, setBenchLoading] = useState(false);
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

  const handleAudit = async () => {
    if (!sourceId) {
      setError('Select a source connection');
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const res = (await api.iamAudit({
        source_id: sourceId,
        workers,
        scan_strategy: scanStrategy,
      })) as { job_id: string };
      navigate(`/jobs/${res.job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start audit');
      setLoading(false);
    }
  };

  const handleMigrate = async () => {
    if (!sourceId || !destId) {
      setError('Source and destination required for migrate');
      return;
    }
    if (skipUserRoles && usersOnly) {
      setError('--skip-user-roles and --users-only are mutually exclusive');
      return;
    }
    setError(null);
    setLoading(true);
    try {
      const res = (await api.iamMigrate({
        source_id: sourceId,
        destination_id: destId,
        workers,
        scan_strategy: scanStrategy,
        dry_run: dryRun,
        skip_user_roles: skipUserRoles,
        users_only: usersOnly,
      })) as { job_id: string };
      navigate(`/jobs/${res.job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to start migration');
      setLoading(false);
    }
  };

  const handleBenchmark = async () => {
    if (!sourceId) {
      setError('Select a source connection for benchmark');
      return;
    }
    setBenchLoading(true);
    setBenchOutput(null);
    setError(null);
    try {
      const res = await api.iamBenchmark({ source_id: sourceId, sample_size: 50, workers: [1, 10, 20] });
      setBenchOutput((res as { output: string }).output || JSON.stringify(res, null, 2));
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Benchmark failed');
    } finally {
      setBenchLoading(false);
    }
  };

  const sourceConns = connections.filter(c => c.role === 'source');
  const destConns = connections.filter(c => c.role === 'destination' || c.role === 'target');

  return (
    <>
      <Title headingLevel="h1" size="2xl">
        IAM Analysis & Migration
      </Title>
      <TextContent style={{ marginBottom: 16 }}>
        <Text>
          Same engine as <code>aap-bridge iam audit / migrate / benchmark</code> — scans the
          permission matrix (resource → role → user/team), detects cross-org sharing, and migrates
          assignments to the target AAP. UI is optional; CLI remains the primary interface.
        </Text>
      </TextContent>

      <Card style={{ marginBottom: 16 }}>
        <CardBody>
          <Split hasGutter style={{ marginBottom: 16 }}>
            <SplitItem isFilled>
              <FormGroup label="Source Connection" fieldId="iam-source" isRequired>
                <FormSelect id="iam-source" value={sourceId} onChange={(_e, v) => setSourceId(v)}>
                  <FormSelectOption value="" label="Select source..." isDisabled />
                  {sourceConns.map(c => (
                    <FormSelectOption key={c.id} value={c.id} label={`${c.name} (${c.url})`} />
                  ))}
                </FormSelect>
              </FormGroup>
            </SplitItem>
            <SplitItem isFilled>
              <FormGroup label="Destination Connection (migrate only)" fieldId="iam-dest">
                <FormSelect id="iam-dest" value={destId} onChange={(_e, v) => setDestId(v)}>
                  <FormSelectOption value="" label="Select destination..." />
                  {destConns.map(c => (
                    <FormSelectOption key={c.id} value={c.id} label={`${c.name} (${c.url})`} />
                  ))}
                </FormSelect>
              </FormGroup>
            </SplitItem>
          </Split>

          <Split hasGutter style={{ marginBottom: 16 }}>
            <SplitItem>
              <FormGroup label="Workers" fieldId="iam-workers">
                <NumberInput
                  value={workers}
                  min={1}
                  max={50}
                  onMinus={() => setWorkers(v => Math.max(1, v - 1))}
                  onPlus={() => setWorkers(v => Math.min(50, v + 1))}
                  onChange={e => {
                    const v = Number((e.target as HTMLInputElement).value);
                    if (!isNaN(v)) setWorkers(Math.max(1, Math.min(50, v)));
                  }}
                  inputName="workers"
                  inputAriaLabel="workers"
                  minusBtnAriaLabel="minus"
                  plusBtnAriaLabel="plus"
                  widthChars={4}
                />
              </FormGroup>
            </SplitItem>
            <SplitItem>
              <FormGroup label="Scan strategy" fieldId="iam-strategy">
                <FormSelect
                  id="iam-strategy"
                  value={scanStrategy}
                  onChange={(_e, v) => setScanStrategy(v as 'resource' | 'principal')}
                >
                  <FormSelectOption value="resource" label="resource — enumerate objects" />
                  <FormSelectOption value="principal" label="principal — enumerate users/teams (faster)" />
                </FormSelect>
              </FormGroup>
            </SplitItem>
          </Split>
        </CardBody>
      </Card>

      <Tabs activeKey={activeTab} onSelect={(_e, k) => setActiveTab(k as string)}>
        <Tab eventKey="audit" title={<TabTitleText>Audit (read-only scan)</TabTitleText>}>
          <div style={{ padding: '16px 0' }}>
            <Card>
              <CardBody>
                <TextContent style={{ marginBottom: 12 }}>
                  <Text>
                    Read-only scan of the source AAP — exports the full permission matrix. No target
                    required. Mirrors <code>aap-bridge iam audit</code>.
                  </Text>
                </TextContent>
                <Button variant="primary" onClick={handleAudit} isDisabled={!sourceId || loading} isLoading={loading}>
                  Run IAM Audit
                </Button>
              </CardBody>
            </Card>
          </div>
        </Tab>
        <Tab eventKey="migrate" title={<TabTitleText>Migrate</TabTitleText>}>
          <div style={{ padding: '16px 0' }}>
            <Card>
              <CardBody>
                <TextContent style={{ marginBottom: 12 }}>
                  <Text>
                    Migrates permissions to the target AAP. Supports two-phase LDAP workflow: first
                    <code> --skip-user-roles</code> (teams only), then <code> --users-only</code> after
                    users log in.
                  </Text>
                </TextContent>
                <Split hasGutter style={{ marginBottom: 12 }}>
                  <SplitItem>
                    <Checkbox
                      id="iam-dry-run"
                      label="Dry run — resolve target IDs without assigning"
                      isChecked={dryRun}
                      onChange={(_e, v) => setDryRun(v)}
                    />
                  </SplitItem>
                  <SplitItem>
                    <Checkbox
                      id="iam-skip-user"
                      label="Skip user roles (teams only)"
                      isChecked={skipUserRoles}
                      onChange={(_e, v) => {
                        setSkipUserRoles(v);
                        if (v) setUsersOnly(false);
                      }}
                    />
                  </SplitItem>
                  <SplitItem>
                    <Checkbox
                      id="iam-users-only"
                      label="Users only (phase 2)"
                      isChecked={usersOnly}
                      onChange={(_e, v) => {
                        setUsersOnly(v);
                        if (v) setSkipUserRoles(false);
                      }}
                    />
                  </SplitItem>
                </Split>
                <Button
                  variant="primary"
                  onClick={handleMigrate}
                  isDisabled={!sourceId || !destId || loading}
                  isLoading={loading}
                >
                  Run IAM Migration
                </Button>
                {skipUserRoles && usersOnly && (
                  <Alert
                    variant="warning"
                    isInline
                    title="--skip-user-roles and --users-only are mutually exclusive"
                    style={{ marginTop: 12 }}
                  />
                )}
              </CardBody>
            </Card>
          </div>
        </Tab>
        <Tab eventKey="benchmark" title={<TabTitleText>Benchmark</TabTitleText>}>
          <div style={{ padding: '16px 0' }}>
            <Card>
              <CardBody>
                <TextContent style={{ marginBottom: 12 }}>
                  <Text>
                    Measures actual API response times and concurrency capacity against the source
                    instance — use to choose the right worker count. Mirrors{' '}
                    <code>aap-bridge iam benchmark</code>.
                  </Text>
                </TextContent>
                <Button variant="secondary" onClick={handleBenchmark} isDisabled={!sourceId || benchLoading} isLoading={benchLoading}>
                  Run Benchmark (50 samples, workers 1/10/20)
                </Button>
                {benchOutput && (
                  <pre
                    style={{
                      marginTop: 16,
                      padding: 12,
                      background: '#f5f5f5',
                      borderRadius: 4,
                      maxHeight: 400,
                      overflow: 'auto',
                      fontSize: '0.85em',
                      whiteSpace: 'pre-wrap',
                    }}
                  >
                    {benchOutput}
                  </pre>
                )}
              </CardBody>
            </Card>
          </div>
        </Tab>
      </Tabs>

      {error && <Alert variant="danger" isInline title={error} style={{ marginTop: 16 }} />}
    </>
  );
}
