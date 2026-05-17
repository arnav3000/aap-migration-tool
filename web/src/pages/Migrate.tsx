import { useState, useEffect, useCallback } from 'react';
import {
  Button,
  Title,
  TextContent,
  Text,
  Alert,
  Label,
  Split,
  SplitItem,
  Flex,
  FlexItem,
  Card,
  CardBody,
  FormGroup,
  FormSelect,
  FormSelectOption,
  Tabs,
  Tab,
  TabTitleText,
  TextInput,
  Spinner,
  Checkbox,
  FormHelperText,
  HelperText,
  HelperTextItem,
} from '@patternfly/react-core';
import TimesIcon from '@patternfly/react-icons/dist/esm/icons/times-icon';
import ExternalLinkAltIcon from '@patternfly/react-icons/dist/esm/icons/external-link-alt-icon';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { LogViewer } from '../components/LogViewer';
import { MigrationProgressView } from '../components/MigrationProgressView';
import { MigrationPreview } from '../components/MigrationPreview';
import { useJobLogs } from '../hooks/useJobLogs';
import type { Connection } from '../types/connection';
import type { MigrationPreviewData } from '../types/resources';

type Step = 'select' | 'preview' | 'run';

export function Migrate() {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [sourceId, setSourceId] = useState('');
  const [destId, setDestId] = useState('');
  const [step, setStep] = useState<Step>('select');
  const [previewJobId, setPreviewJobId] = useState('');
  const [runJobId, setRunJobId] = useState('');
  const [previewData, setPreviewData] = useState<MigrationPreviewData | null>(null);
  const [previewError, setPreviewError] = useState('');
  const [loading, setLoading] = useState(false);
  const [exclude, setExclude] = useState<Record<string, string[]>>({});
  const [cancelling, setCancelling] = useState(false);
  const [migrationDone, setMigrationDone] = useState(false);
  const [runActiveTab, setRunActiveTab] = useState<string>('output');
  const [orgs, setOrgs] = useState<{ id: number; name: string; description: string }[]>([]);
  const [selectedOrgIds, setSelectedOrgIds] = useState<number[]>([]);
  const [orgsLoading, setOrgsLoading] = useState(false);
  const [namePrefix, setNamePrefix] = useState('');
  const navigate = useNavigate();

  const jobLogs = useJobLogs(runJobId);

  const loadConnections = useCallback(async () => {
    const conns = await api.listConnections() as Connection[];
    setConnections(conns);
  }, []);

  useEffect(() => { loadConnections(); }, [loadConnections]);

  useEffect(() => {
    if (!sourceId) {
      setOrgs([]);
      setSelectedOrgIds([]);
      return;
    }
    let cancelled = false;
    const fetchOrgs = async () => {
      setOrgsLoading(true);
      try {
        const result = await api.listOrganizations(sourceId);
        if (!cancelled) setOrgs(result);
      } catch {
        if (!cancelled) setOrgs([]);
      } finally {
        if (!cancelled) setOrgsLoading(false);
      }
    };
    fetchOrgs();
    return () => { cancelled = true; };
  }, [sourceId]);

  const handlePreview = async () => {
    if (!sourceId || !destId) return;
    if (sourceId === destId) return;

    setLoading(true);
    setPreviewData(null);
    setPreviewError('');
    setExclude({});

    try {
      const orgIds = selectedOrgIds.length > 0 ? selectedOrgIds : undefined;
      const result = await api.migrationPreview(sourceId, destId, orgIds);
      setPreviewJobId(result.job_id);
      setStep('preview');
      pollPreview(result.job_id);
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const pollPreview = async (jobId: string) => {
    const poll = async () => {
      try {
        const resp = await api.getMigrationPreview(jobId) as Record<string, unknown>;
        if (resp.status === 'running' || resp.status === 'pending') {
          setTimeout(poll, 1500);
          return;
        }
        if (resp.status === 'failed') {
          const errMsg = resp.error;
          setPreviewError(typeof errMsg === 'string' ? errMsg : JSON.stringify(errMsg) || 'Preview failed');
          return;
        }
        if (resp.resources) {
          setPreviewData(resp as unknown as MigrationPreviewData);
        } else {
          setPreviewError('Preview completed but returned no resource data');
        }
      } catch {
        setTimeout(poll, 1500);
      }
    };
    setTimeout(poll, 2000);
  };

  const handleRun = async () => {
    if (!previewJobId) return;
    setLoading(true);
    setCancelling(false);
    setMigrationDone(false);
    try {
      const orgIds = selectedOrgIds.length > 0 ? selectedOrgIds : undefined;
      const prefix = namePrefix.trim() || undefined;
      const result = await api.migrationRun(sourceId, destId, previewJobId, exclude, orgIds, prefix);
      setRunJobId(result.job_id);
      setStep('run');
    } catch (err) {
      setPreviewError(err instanceof Error ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  };

  const handleCancel = async () => {
    if (!runJobId || cancelling) return;
    setCancelling(true);
    try {
      await api.cancelJob(runJobId);
    } catch {
      // Job may have already finished
    }
  };

  const handleBack = () => {
    setStep('select');
    setPreviewJobId('');
    setRunJobId('');
    setPreviewData(null);
    setPreviewError('');
    setExclude({});
    setCancelling(false);
    setMigrationDone(false);
    setRunActiveTab('output');
    setNamePrefix('');
  };

  const handleLogClose = (status: string) => {
    setMigrationDone(true);
    if (status === 'cancelled') {
      setCancelling(false);
    }
  };

  const TERMINAL_STATUSES = ['completed', 'failed', 'cancelled'];

  useEffect(() => {
    if (!runJobId) return;
    const s = jobLogs.status;
    if (TERMINAL_STATUSES.includes(s)) {
      setMigrationDone(true);
      if (s === 'cancelled') setCancelling(false);
    }
  }, [runJobId, jobLogs.status]);

  const sourceConn = connections.find(c => c.id === sourceId);
  const destConn = connections.find(c => c.id === destId);

  return (
    <>
      <Title headingLevel="h1" size="2xl">Migrate</Title>
      <TextContent style={{ marginBottom: 16 }}>
        <Text>Migrate resources from a source AWX/AAP platform to a destination AAP platform.</Text>
      </TextContent>

      {connections.length < 2 && (
        <Alert variant="info" isInline title="You need at least 2 connections configured to perform a migration." />
      )}

      {step === 'select' && (
        <Card>
          <CardBody>
            <Flex direction={{ default: 'column' }} spaceItems={{ default: 'spaceItemsMd' }}>
              <FlexItem>
                <FormGroup label="Source" fieldId="source-select">
                  <FormSelect
                    id="source-select"
                    value={sourceId}
                    onChange={(_e, val) => setSourceId(val)}
                    aria-label="Select source connection"
                  >
                    <FormSelectOption key="" value="" label="-- Select source --" isDisabled />
                    {connections.map(c => (
                      <FormSelectOption
                        key={c.id}
                        value={c.id}
                        label={`${c.name} (${c.type.toUpperCase()} — ${c.url})`}
                        isDisabled={c.id === destId}
                      />
                    ))}
                  </FormSelect>
                </FormGroup>
              </FlexItem>
              <FlexItem>
                <FormGroup label="Destination" fieldId="dest-select">
                  <FormSelect
                    id="dest-select"
                    value={destId}
                    onChange={(_e, val) => setDestId(val)}
                    aria-label="Select destination connection"
                  >
                    <FormSelectOption key="" value="" label="-- Select destination (AAP only) --" isDisabled />
                    {connections.filter(c => c.type === 'aap').map(c => (
                      <FormSelectOption
                        key={c.id}
                        value={c.id}
                        label={`${c.name} (${c.type.toUpperCase()} — ${c.url})`}
                        isDisabled={c.id === sourceId}
                      />
                    ))}
                  </FormSelect>
                </FormGroup>
              </FlexItem>
              {sourceId && destId && sourceId === destId && (
                <FlexItem>
                  <Alert variant="danger" isInline title="Source and destination cannot be the same connection." />
                </FlexItem>
              )}
              {sourceId && (
                <FlexItem>
                  <FormGroup
                    label="Organizations (optional — leave empty for all)"
                    fieldId="org-select"
                  >
                    {orgsLoading ? (
                      <Spinner size="md" />
                    ) : orgs.length === 0 ? (
                      <Text component="small">No organizations found on source</Text>
                    ) : (
                      <div style={{ maxHeight: 200, overflow: 'auto', border: '1px solid var(--pf-v5-global--BorderColor--100)', borderRadius: 4, padding: 8 }}>
                        {orgs.map(org => (
                          <Checkbox
                            key={org.id}
                            id={`org-${org.id}`}
                            label={`${org.name}${org.description ? ` — ${org.description}` : ''}`}
                            isChecked={selectedOrgIds.includes(org.id)}
                            onChange={(_e, checked) => {
                              setSelectedOrgIds(prev =>
                                checked ? [...prev, org.id] : prev.filter(id => id !== org.id)
                              );
                            }}
                          />
                        ))}
                      </div>
                    )}
                    <FormHelperText>
                      <HelperText>
                        <HelperTextItem>Select specific organizations to migrate, or leave unchecked to migrate everything.</HelperTextItem>
                      </HelperText>
                    </FormHelperText>
                  </FormGroup>
                </FlexItem>
              )}
              <FlexItem>
                <FormGroup
                  label="Name Prefix (optional)"
                  fieldId="name-prefix"
                >
                  <TextInput
                    id="name-prefix"
                    value={namePrefix}
                    onChange={(_e, val) => setNamePrefix(val)}
                    placeholder="e.g. prod-east-"
                  />
                  <FormHelperText>
                    <HelperText>
                      <HelperTextItem>Prepend this value to all migrated object names. Useful when consolidating multiple AAP instances.</HelperTextItem>
                    </HelperText>
                  </FormHelperText>
                </FormGroup>
              </FlexItem>
              <FlexItem>
                <Button
                  variant="primary"
                  onClick={handlePreview}
                  isDisabled={!sourceId || !destId || sourceId === destId || loading}
                  isLoading={loading}
                >
                  Preview Migration
                </Button>
              </FlexItem>
            </Flex>
          </CardBody>
        </Card>
      )}

      {step === 'preview' && (
        <>
          <Card style={{ marginBottom: 16 }}>
            <CardBody>
              <Split hasGutter>
                <SplitItem>
                  <Label color="blue" isCompact>Source</Label>{' '}
                  {sourceConn?.name} ({sourceConn?.type.toUpperCase()})
                </SplitItem>
                <SplitItem>&rarr;</SplitItem>
                <SplitItem>
                  <Label color="purple" isCompact>Destination</Label>{' '}
                  {destConn?.name} ({destConn?.type.toUpperCase()})
                </SplitItem>
              </Split>
            </CardBody>
          </Card>

          {previewJobId && (
            <div style={{ marginBottom: 16 }}>
              <Split hasGutter>
                <SplitItem isFilled>
                  <Title headingLevel="h3">Preview Log</Title>
                </SplitItem>
                <SplitItem>
                  <Button
                    variant="link"
                    icon={<ExternalLinkAltIcon />}
                    onClick={() => navigate(`/jobs/${previewJobId}`)}
                  >
                    Open in Jobs
                  </Button>
                </SplitItem>
              </Split>
              <LogViewer jobId={previewJobId} />
            </div>
          )}

          {previewError && (
            <Alert variant="danger" isInline title={previewError} style={{ marginBottom: 16 }} />
          )}

          {previewData && (
            <div style={{ marginBottom: 16 }}>
              <Title headingLevel="h3" style={{ marginBottom: 8 }}>Preview Results</Title>
              <MigrationPreview
                preview={previewData}
                exclude={exclude}
                onExcludeChange={setExclude}
              />
            </div>
          )}

          <Flex>
            <FlexItem>
              <Button variant="secondary" onClick={handleBack}>Back</Button>
            </FlexItem>
            {previewData && (
              <FlexItem>
                <Button
                  variant="primary"
                  onClick={handleRun}
                  isDisabled={loading}
                  isLoading={loading}
                >
                  Start Migration
                </Button>
              </FlexItem>
            )}
          </Flex>
        </>
      )}

      {step === 'run' && (
        <>
          <Card style={{ marginBottom: 16 }}>
            <CardBody>
              <Split hasGutter>
                <SplitItem>
                  <Label color="blue" isCompact>Source</Label>{' '}
                  {sourceConn?.name}
                </SplitItem>
                <SplitItem>&rarr;</SplitItem>
                <SplitItem>
                  <Label color="purple" isCompact>Destination</Label>{' '}
                  {destConn?.name}
                </SplitItem>
              </Split>
            </CardBody>
          </Card>

          {runJobId && (
            <div style={{ marginBottom: 16, display: 'flex', flexDirection: 'column', flex: 1, minHeight: 0 }}>
              <Split hasGutter style={{ marginBottom: 12 }}>
                <SplitItem isFilled>
                  <Title headingLevel="h3">Migration Output</Title>
                </SplitItem>
                <SplitItem>
                  {!migrationDone && (
                    <Button
                      variant="danger"
                      onClick={handleCancel}
                      isDisabled={cancelling}
                      isLoading={cancelling}
                    >
                      {cancelling ? 'Cancelling...' : 'Cancel Migration'}
                    </Button>
                  )}
                </SplitItem>
                <SplitItem>
                  <Button
                    variant="link"
                    icon={<ExternalLinkAltIcon />}
                    onClick={() => navigate(`/jobs/${runJobId}`)}
                  >
                    Open in Jobs
                  </Button>
                </SplitItem>
                <SplitItem>
                  <Button variant="plain" aria-label="Back" onClick={handleBack}>
                    <TimesIcon />
                  </Button>
                </SplitItem>
              </Split>
              <Tabs activeKey={runActiveTab} onSelect={(_e, k) => setRunActiveTab(k as string)} style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
                <Tab eventKey="output" title={<TabTitleText>Output</TabTitleText>} style={{ flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
                  <div style={{ padding: '16px 0', flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
                    <MigrationProgressView
                      events={jobLogs.events}
                      jobStatus={jobLogs.status}
                    />
                  </div>
                </Tab>
                <Tab eventKey="logs" title={<TabTitleText>Logs</TabTitleText>}>
                  <div style={{ padding: '16px 0', flex: 1, display: 'flex', flexDirection: 'column', minHeight: 0 }}>
                    <LogViewer jobId={runJobId} externalLines={jobLogs.textLines} externalStatus={jobLogs.status} onClose={handleLogClose} fullPage />
                  </div>
                </Tab>
              </Tabs>
            </div>
          )}

          <Button variant="secondary" onClick={handleBack} style={{ marginTop: 16 }}>
            New Migration
          </Button>
        </>
      )}
    </>
  );
}
