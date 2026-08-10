import { useState, useEffect, useCallback, useMemo } from 'react';
import {
  Button,
  Checkbox,
  Divider,
  Title,
  TextContent,
  Text,
  TextInput,
  Alert,
  Label,
  Modal,
  ModalVariant,
  Split,
  SplitItem,
  Flex,
  FlexItem,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  MenuToggle,
  Select,
  SelectOption,
  SelectList,
  Spinner,
} from '@patternfly/react-core';
import TimesIcon from '@patternfly/react-icons/dist/esm/icons/times-icon';
import ExternalLinkAltIcon from '@patternfly/react-icons/dist/esm/icons/external-link-alt-icon';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import { LogViewer } from '../components/LogViewer';
import type { Connection } from '../types/connection';

interface ActiveJob {
  id: string;
  connName: string;
  operation: string;
}

interface JTResource {
  id: number;
  name: string;
  organization?: number;
  summary_fields?: {
    organization?: { id: number; name: string };
    project?: { id: number; name: string };
  };
}

interface WFResource {
  id: number;
  name: string;
  summary_fields?: {
    organization?: { id: number; name: string };
    inventory?: { id: number; name: string };
  };
}

export function Operations() {
  const [connections, setConnections] = useState<Connection[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [activeJobs, setActiveJobs] = useState<ActiveJob[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [cleanupConfirmOpen, setCleanupConfirmOpen] = useState(false);
  const [cleanupAcknowledged, setCleanupAcknowledged] = useState(false);
  const [cleanupTargetId, setCleanupTargetId] = useState<string | null>(null);
  const navigate = useNavigate();

  // Selective JT migration state
  const [smSourceId, setSmSourceId] = useState<string | null>(null);
  const [smSourceOpen, setSmSourceOpen] = useState(false);
  const [smDestId, setSmDestId] = useState<string | null>(null);
  const [smDestOpen, setSmDestOpen] = useState(false);
  const [smJTs, setSmJTs] = useState<JTResource[]>([]);
  const [smJTsLoading, setSmJTsLoading] = useState(false);
  const [smSelectedJTIds, setSmSelectedJTIds] = useState<Set<number>>(new Set());
  const [smWFs, setSmWFs] = useState<WFResource[]>([]);
  const [smWFsLoading, setSmWFsLoading] = useState(false);
  const [smSelectedWFIds, setSmSelectedWFIds] = useState<Set<number>>(new Set());
  const [smFilter, setSmFilter] = useState('');
  const [smError, setSmError] = useState<string | null>(null);
  const [smRunning, setSmRunning] = useState(false);
  const [smForceUpdate, setSmForceUpdate] = useState(false);
  const [smNamePrefix, setSmNamePrefix] = useState('');

  const loadConnections = useCallback(async () => {
    const conns = await api.listConnections() as Connection[];
    setConnections(conns);
  }, []);

  useEffect(() => { loadConnections(); }, [loadConnections]);

  const handleOperation = async (id: string, op: 'cleanup' | 'scan') => {
    setError(null);
    try {
      let result: { job_id: string };
      switch (op) {
        case 'cleanup': result = await api.runCleanup(id); break;
        case 'scan': result = await api.runResourceScan(id); break;
      }
      const conn = connections.find(c => c.id === id);
      setActiveJobs(prev => [...prev, {
        id: result.job_id,
        connName: conn?.name || id,
        operation: op,
      }]);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    }
  };

  const dismissJob = (jobId: string) => {
    setActiveJobs(prev => prev.filter(j => j.id !== jobId));
  };

  const selected = connections.find(c => c.id === selectedId);
  const sources = useMemo(() => connections.filter(c => c.role === 'source'), [connections]);
  const destinations = useMemo(
    () => connections.filter(c => c.role === 'destination' || c.role === 'target'),
    [connections],
  );

  // Load job templates and workflows when source selection changes
  useEffect(() => {
    if (!smSourceId) {
      setSmJTs([]);
      setSmSelectedJTIds(new Set());
      setSmWFs([]);
      setSmSelectedWFIds(new Set());
      return;
    }
    let cancelled = false;
    setSmJTsLoading(true);
    setSmWFsLoading(true);
    setSmError(null);
    api.listResources(smSourceId, 'job_templates')
      .then(data => {
        if (cancelled) return;
        setSmJTs(data as JTResource[]);
        setSmSelectedJTIds(new Set());
      })
      .catch(err => {
        if (cancelled) return;
        setSmError(err instanceof Error ? err.message : String(err));
        setSmJTs([]);
      })
      .finally(() => { if (!cancelled) setSmJTsLoading(false); });
    api.listResources(smSourceId, 'workflow_job_templates')
      .then(data => {
        if (cancelled) return;
        setSmWFs(data as WFResource[]);
        setSmSelectedWFIds(new Set());
      })
      .catch(err => {
        if (cancelled) return;
        setSmError(err instanceof Error ? err.message : String(err));
        setSmWFs([]);
      })
      .finally(() => { if (!cancelled) setSmWFsLoading(false); });
    return () => { cancelled = true; };
  }, [smSourceId]);

  const filteredJTs = useMemo(() => {
    if (!smFilter) return smJTs;
    const lower = smFilter.toLowerCase();
    return smJTs.filter(jt =>
      jt.name.toLowerCase().includes(lower)
      || jt.summary_fields?.organization?.name?.toLowerCase().includes(lower)
      || jt.summary_fields?.project?.name?.toLowerCase().includes(lower)
    );
  }, [smJTs, smFilter]);

  const filteredWFs = useMemo(() => {
    if (!smFilter) return smWFs;
    const lower = smFilter.toLowerCase();
    return smWFs.filter(wf =>
      wf.name.toLowerCase().includes(lower)
      || wf.summary_fields?.organization?.name?.toLowerCase().includes(lower)
      || wf.summary_fields?.inventory?.name?.toLowerCase().includes(lower)
    );
  }, [smWFs, smFilter]);

  const smSelectedCount = smSelectedJTIds.size + smSelectedWFIds.size;

  const toggleWF = (id: number) => {
    setSmSelectedWFIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAllFilteredWF = () => {
    const filteredIds = new Set(filteredWFs.map(wf => wf.id));
    const allSelected = filteredWFs.every(wf => smSelectedWFIds.has(wf.id));
    setSmSelectedWFIds(prev => {
      const next = new Set(prev);
      if (allSelected) {
        filteredIds.forEach(id => next.delete(id));
      } else {
        filteredIds.forEach(id => next.add(id));
      }
      return next;
    });
  };

  const toggleJT = (id: number) => {
    setSmSelectedJTIds(prev => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const toggleAllFiltered = () => {
    const filteredIds = new Set(filteredJTs.map(jt => jt.id));
    const allSelected = filteredJTs.every(jt => smSelectedJTIds.has(jt.id));
    setSmSelectedJTIds(prev => {
      const next = new Set(prev);
      if (allSelected) {
        filteredIds.forEach(id => next.delete(id));
      } else {
        filteredIds.forEach(id => next.add(id));
      }
      return next;
    });
  };

  const handleSelectiveMigrate = async () => {
    if (!smSourceId || !smDestId || smSelectedCount === 0) return;
    setSmError(null);
    setSmRunning(true);
    try {
      const result = await api.selectiveMigrate(
        smSourceId,
        smDestId,
        Array.from(smSelectedJTIds),
        Array.from(smSelectedWFIds),
        smForceUpdate,
        smNamePrefix.trim() || undefined,
      );
      const srcConn = connections.find(c => c.id === smSourceId);
      const destConn = connections.find(c => c.id === smDestId);
      setActiveJobs(prev => [...prev, {
        id: result.job_id,
        connName: `${srcConn?.name || smSourceId} → ${destConn?.name || smDestId}`,
        operation: 'selective-migration',
      }]);
    } catch (err) {
      setSmError(err instanceof Error ? err.message : String(err));
    } finally {
      setSmRunning(false);
    }
  };

  return (
    <>
      <Title headingLevel="h1" size="2xl">Operations</Title>
      <TextContent style={{ marginBottom: 16 }}>
        <Text>Select a connection and run operations against it.</Text>
      </TextContent>

      {connections.length === 0 && (
        <Alert variant="info" isInline title="No connections configured. Add connections first." />
      )}

      {sources.length > 0 && (
        <>
          <Title headingLevel="h2" size="lg" style={{ marginBottom: 8 }}>Sources</Title>
          <Flex style={{ marginBottom: 16 }}>
            {sources.map(conn => (
              <FlexItem key={conn.id}>
                <Button
                  variant={selectedId === conn.id ? 'primary' : 'secondary'}
                  onClick={() => setSelectedId(conn.id)}
                >
                  <Split hasGutter>
                    <SplitItem>{conn.name}</SplitItem>
                    <SplitItem>
                      <Label color={conn.type === 'awx' ? 'blue' : 'purple'} isCompact>
                        {conn.type.toUpperCase()}
                      </Label>
                    </SplitItem>
                  </Split>
                </Button>
              </FlexItem>
            ))}
          </Flex>
        </>
      )}

      {destinations.length > 0 && (
        <>
          <Title headingLevel="h2" size="lg" style={{ marginBottom: 8 }}>Destinations</Title>
          <Flex style={{ marginBottom: 16 }}>
            {destinations.map(conn => (
              <FlexItem key={conn.id}>
                <Button
                  variant={selectedId === conn.id ? 'primary' : 'secondary'}
                  onClick={() => setSelectedId(conn.id)}
                >
                  <Split hasGutter>
                    <SplitItem>{conn.name}</SplitItem>
                    <SplitItem>
                      <Label color={conn.type === 'awx' ? 'blue' : 'purple'} isCompact>
                        {conn.type.toUpperCase()}
                      </Label>
                    </SplitItem>
                  </Split>
                </Button>
              </FlexItem>
            ))}
          </Flex>
        </>
      )}

      {error && (
        <Alert variant="danger" isInline title={error} style={{ marginBottom: 16 }} />
      )}

      {selected && selected.ping_status === 'error' && (
        <Alert
          variant="warning"
          isInline
          title={`Connection "${selected.name}" is unreachable${selected.ping_error ? ': ' + selected.ping_error : ''}`}
          style={{ marginBottom: 16 }}
        />
      )}

      {selected && selected.auth_status === 'error' && (
        <Alert
          variant="warning"
          isInline
          title={`Connection "${selected.name}" authentication failed${selected.auth_error ? ': ' + selected.auth_error : ''}`}
          style={{ marginBottom: 16 }}
        />
      )}

      {selected && (
        <Card>
          <CardHeader>
            <CardTitle>
              <Split hasGutter>
                <SplitItem>{selected.name}</SplitItem>
                <SplitItem>
                  <Label color={selected.type === 'awx' ? 'blue' : 'purple'}>{selected.type.toUpperCase()}</Label>
                </SplitItem>
                <SplitItem>
                  <Label isCompact>{selected.url}</Label>
                </SplitItem>
              </Split>
            </CardTitle>
          </CardHeader>
          <CardBody>
            <Flex>
              <FlexItem>
                <Button variant="secondary" onClick={() => navigate(`/browse?conn=${selected.id}`)}>Browse</Button>
              </FlexItem>
              <FlexItem>
                <Button
                  variant="secondary"
                  title="Count exportable resources on this connection (does not write to disk)"
                  onClick={() => handleOperation(selected.id, 'scan')}
                >
                  Scan Resources
                </Button>
              </FlexItem>
              <FlexItem>
                <Button variant="danger" onClick={() => {
                  setCleanupTargetId(selected.id);
                  setCleanupAcknowledged(false);
                  setCleanupConfirmOpen(true);
                }}>Cleanup</Button>
              </FlexItem>
            </Flex>
          </CardBody>
        </Card>
      )}

      {/* Selective JT Migration */}
      {sources.length > 0 && destinations.length > 0 && (
        <>
          <Divider style={{ marginTop: 32, marginBottom: 24 }} />
          <Title headingLevel="h2" size="xl" style={{ marginBottom: 8 }}>Selective Template Migration</Title>
          <TextContent style={{ marginBottom: 16 }}>
            <Text>Pick job templates or workflows from a source and migrate them with all their dependencies to a destination.</Text>
            {import.meta.env.VITE_GIT_COMMIT ? (
              <Text component="small">UI build {import.meta.env.VITE_GIT_COMMIT}</Text>
            ) : null}
          </TextContent>

          <Card style={{ marginBottom: 16 }}>
            <CardBody>
              <Flex style={{ gap: 16, marginBottom: 16 }}>
                <FlexItem>
                  <FormGroup label="Source" fieldId="sm-source">
                    <Select
                      isOpen={smSourceOpen}
                      selected={smSourceId || undefined}
                      onSelect={(_e, val) => { setSmSourceId(val as string); setSmSourceOpen(false); }}
                      onOpenChange={setSmSourceOpen}
                      toggle={(toggleRef) => (
                        <MenuToggle ref={toggleRef} onClick={() => setSmSourceOpen(prev => !prev)} isExpanded={smSourceOpen} style={{ minWidth: 220 }}>
                          {sources.find(c => c.id === smSourceId)?.name || 'Select source...'}
                        </MenuToggle>
                      )}
                    >
                      <SelectList>
                        {sources.map(c => (
                          <SelectOption key={c.id} value={c.id}>{c.name}</SelectOption>
                        ))}
                      </SelectList>
                    </Select>
                  </FormGroup>
                </FlexItem>
                <FlexItem>
                  <FormGroup label="Destination" fieldId="sm-destination">
                    <Select
                      isOpen={smDestOpen}
                      selected={smDestId || undefined}
                      onSelect={(_e, val) => { setSmDestId(val as string); setSmDestOpen(false); }}
                      onOpenChange={setSmDestOpen}
                      toggle={(toggleRef) => (
                        <MenuToggle ref={toggleRef} onClick={() => setSmDestOpen(prev => !prev)} isExpanded={smDestOpen} style={{ minWidth: 220 }}>
                          {destinations.find(c => c.id === smDestId)?.name || 'Select destination...'}
                        </MenuToggle>
                      )}
                    >
                      <SelectList>
                        {destinations.map(c => (
                          <SelectOption key={c.id} value={c.id}>{c.name}</SelectOption>
                        ))}
                      </SelectList>
                    </Select>
                  </FormGroup>
                </FlexItem>
              </Flex>
            </CardBody>
          </Card>

          {smError && (
            <Alert variant="danger" isInline title={smError} style={{ marginBottom: 16 }} />
          )}

          {smSourceId && (smJTsLoading || smWFsLoading) && (
            <Flex style={{ padding: 24 }}>
              <FlexItem><Spinner size="md" /></FlexItem>
              <FlexItem><Text>Loading templates...</Text></FlexItem>
            </Flex>
          )}

          {smSourceId && !smJTsLoading && smJTs.length > 0 && (
            <Card style={{ marginBottom: 16 }}>
              <CardHeader>
                <CardTitle>
                  <Split hasGutter>
                    <SplitItem isFilled>
                      Job Templates ({smSelectedJTIds.size} of {smJTs.length} selected)
                    </SplitItem>
                    <SplitItem>
                      <TextInput
                        type="search"
                        aria-label="Filter job templates"
                        placeholder="Filter..."
                        value={smFilter}
                        onChange={(_e, val) => setSmFilter(val)}
                        style={{ width: 250 }}
                      />
                    </SplitItem>
                  </Split>
                </CardTitle>
              </CardHeader>
              <CardBody style={{ maxHeight: 400, overflowY: 'auto', padding: 0 }}>
                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--pf-v5-global--BorderColor--100, #d2d2d2)' }}>
                      <th style={{ padding: '8px 16px', width: 40 }}>
                        <input
                          type="checkbox"
                          aria-label="Select all"
                          checked={filteredJTs.length > 0 && filteredJTs.every(jt => smSelectedJTIds.has(jt.id))}
                          onChange={toggleAllFiltered}
                        />
                      </th>
                      <th style={{ padding: '8px 16px', textAlign: 'left' }}>Name</th>
                      <th style={{ padding: '8px 16px', textAlign: 'left' }}>Organization</th>
                      <th style={{ padding: '8px 16px', textAlign: 'left' }}>Project</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredJTs.map(jt => (
                      <tr
                        key={jt.id}
                        style={{
                          borderBottom: '1px solid var(--pf-v5-global--BorderColor--100, #d2d2d2)',
                          background: smSelectedJTIds.has(jt.id) ? 'var(--pf-v5-global--BackgroundColor--200, #f0f0f0)' : undefined,
                          cursor: 'pointer',
                        }}
                        onClick={() => toggleJT(jt.id)}
                      >
                        <td style={{ padding: '8px 16px' }}>
                          <input
                            type="checkbox"
                            checked={smSelectedJTIds.has(jt.id)}
                            onChange={() => toggleJT(jt.id)}
                            aria-label={`Select ${jt.name}`}
                            onClick={e => e.stopPropagation()}
                          />
                        </td>
                        <td style={{ padding: '8px 16px' }}>{jt.name}</td>
                        <td style={{ padding: '8px 16px' }}>{jt.summary_fields?.organization?.name || '—'}</td>
                        <td style={{ padding: '8px 16px' }}>{jt.summary_fields?.project?.name || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardBody>
            </Card>
          )}

          {smSourceId && !smJTsLoading && smJTs.length === 0 && !smWFsLoading && smWFs.length === 0 && !smError && (
            <Alert variant="info" isInline title="No job templates or workflows found on this source." style={{ marginBottom: 16 }} />
          )}

          {smSourceId && !smWFsLoading && smWFs.length > 0 && (
            <Card style={{ marginBottom: 16 }}>
              <CardHeader>
                <CardTitle>
                  <Split hasGutter>
                    <SplitItem isFilled>
                      Workflows ({smSelectedWFIds.size} of {smWFs.length} selected)
                    </SplitItem>
                  </Split>
                </CardTitle>
              </CardHeader>
              <CardBody style={{ maxHeight: 300, overflowY: 'auto', padding: 0 }}>
                <table style={{ width: '100%', borderCollapse: 'collapse' }}>
                  <thead>
                    <tr style={{ borderBottom: '1px solid var(--pf-v5-global--BorderColor--100, #d2d2d2)' }}>
                      <th style={{ padding: '8px 16px', width: 40 }}>
                        <input
                          type="checkbox"
                          aria-label="Select all workflows"
                          checked={filteredWFs.length > 0 && filteredWFs.every(wf => smSelectedWFIds.has(wf.id))}
                          onChange={toggleAllFilteredWF}
                        />
                      </th>
                      <th style={{ padding: '8px 16px', textAlign: 'left' }}>Name</th>
                      <th style={{ padding: '8px 16px', textAlign: 'left' }}>Organization</th>
                      <th style={{ padding: '8px 16px', textAlign: 'left' }}>Inventory</th>
                    </tr>
                  </thead>
                  <tbody>
                    {filteredWFs.map(wf => (
                      <tr
                        key={wf.id}
                        style={{
                          borderBottom: '1px solid var(--pf-v5-global--BorderColor--100, #d2d2d2)',
                          background: smSelectedWFIds.has(wf.id) ? 'var(--pf-v5-global--BackgroundColor--200, #f0f0f0)' : undefined,
                          cursor: 'pointer',
                        }}
                        onClick={() => toggleWF(wf.id)}
                      >
                        <td style={{ padding: '8px 16px' }}>
                          <input
                            type="checkbox"
                            checked={smSelectedWFIds.has(wf.id)}
                            onChange={() => toggleWF(wf.id)}
                            aria-label={`Select workflow ${wf.name}`}
                            onClick={e => e.stopPropagation()}
                          />
                        </td>
                        <td style={{ padding: '8px 16px' }}>{wf.name}</td>
                        <td style={{ padding: '8px 16px' }}>{wf.summary_fields?.organization?.name || '—'}</td>
                        <td style={{ padding: '8px 16px' }}>{wf.summary_fields?.inventory?.name || '—'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </CardBody>
            </Card>
          )}

          <Card style={{ marginBottom: 16 }}>
            <CardBody>
              <Flex alignItems={{ default: 'alignItemsFlexEnd' }} style={{ gap: 24, flexWrap: 'wrap' }}>
                <FlexItem style={{ minWidth: 280, flexGrow: 1 }}>
                  <FormGroup label="Name Prefix (optional)" fieldId="sm-name-prefix">
                    <TextInput
                      id="sm-name-prefix"
                      aria-label="Name Prefix (optional)"
                      value={smNamePrefix}
                      onChange={(_e, val) => setSmNamePrefix(val)}
                      placeholder="e.g. dev_"
                    />
                    <FormHelperText>
                      <HelperText>
                        <HelperTextItem>
                          Prepended to selected templates and every dependency created on the target
                          (projects, credentials, inventories, etc.).
                        </HelperTextItem>
                      </HelperText>
                    </FormHelperText>
                  </FormGroup>
                </FlexItem>
                <FlexItem>
                  <Button
                    variant="primary"
                    isDisabled={!smSourceId || !smDestId || smSelectedCount === 0 || smRunning}
                    isLoading={smRunning}
                    onClick={handleSelectiveMigrate}
                  >
                    Migrate {smSelectedCount} template{smSelectedCount !== 1 ? 's' : ''}
                  </Button>
                </FlexItem>
                <FlexItem style={{ paddingBottom: 8 }}>
                  <Checkbox
                    id="sm-force-update"
                    label="Force update (re-migrate previously imported resources)"
                    isChecked={smForceUpdate}
                    onChange={(_event, checked) => setSmForceUpdate(checked)}
                  />
                </FlexItem>
              </Flex>
            </CardBody>
          </Card>
        </>
      )}

      {activeJobs.map(job => (
        <div key={job.id} style={{ marginTop: 24 }}>
          <Split hasGutter>
            <SplitItem isFilled>
              <Title headingLevel="h3">
                {job.connName} — {job.operation}
              </Title>
            </SplitItem>
            <SplitItem>
              <Button
                variant="link"
                icon={<ExternalLinkAltIcon />}
                onClick={() => navigate(`/jobs/${job.id}`)}
              >
                Open in Jobs
              </Button>
            </SplitItem>
            <SplitItem>
              <Button variant="plain" aria-label="Dismiss" onClick={() => dismissJob(job.id)}>
                <TimesIcon />
              </Button>
            </SplitItem>
          </Split>
          <LogViewer jobId={job.id} />
        </div>
      ))}

      <Modal
        variant={ModalVariant.small}
        isOpen={cleanupConfirmOpen}
        onClose={() => setCleanupConfirmOpen(false)}
        title="Confirm Cleanup"
        titleIconVariant="warning"
        actions={[
          <Button
            key="confirm"
            variant="danger"
            isDisabled={!cleanupAcknowledged}
            onClick={() => {
              setCleanupConfirmOpen(false);
              if (cleanupTargetId) {
                handleOperation(cleanupTargetId, 'cleanup');
              }
            }}
          >
            Confirm Cleanup
          </Button>,
          <Button key="cancel" variant="link" onClick={() => setCleanupConfirmOpen(false)}>
            Cancel
          </Button>,
        ]}
      >
        <Alert variant="danger" isInline isPlain title="This operation is destructive and cannot be undone." style={{ marginBottom: 16 }} />
        <Text component="p" style={{ marginBottom: 16 }}>
          Cleanup will <strong>permanently delete all resources</strong> from the selected connection.
          This includes organizations, teams, users, credentials, projects, inventories, job templates,
          and all other managed resources.
        </Text>
        <Checkbox
          id="cleanup-acknowledge"
          label="I understand this will permanently delete all resources on this connection"
          isChecked={cleanupAcknowledged}
          onChange={(_e, checked) => setCleanupAcknowledged(checked)}
        />
      </Modal>
    </>
  );
}
