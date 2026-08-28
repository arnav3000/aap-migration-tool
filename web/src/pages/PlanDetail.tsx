import { useState, useEffect, useCallback } from 'react';
import {
  Title,
  TextContent,
  Text,
  Button,
  Card,
  CardBody,
  CardTitle,
  Alert,
  Label,
  Split,
  SplitItem,
  Flex,
  FlexItem,
  FormGroup,
  FormSelect,
  FormSelectOption,
  TextInput,
  Spinner,
  Tabs,
  Tab,
  TabTitleText,
} from '@patternfly/react-core';
import { Table, Thead, Tbody, Tr, Th, Td } from '@patternfly/react-table';
import PlusCircleIcon from '@patternfly/react-icons/dist/esm/icons/plus-circle-icon';
import TimesIcon from '@patternfly/react-icons/dist/esm/icons/times-icon';
import { useNavigate, useParams } from 'react-router-dom';
import { api } from '../api/client';
import { PhaseEditor } from '../components/PhaseEditor';
import type { AnalysisDataMap } from '../components/PhaseEditor';
import type { Connection } from '../types/connection';
import type { MigrationPlan, PlanPhase, PlanSource } from '../types/resources';

interface AnalysisJob {
  id: string;
  seq_id?: number;
  name: string;
  status: string;
  started_at: string;
  finished_at?: string;
}

export function PlanDetail() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const [plan, setPlan] = useState<MigrationPlan | null>(null);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [analysisJobs, setAnalysisJobs] = useState<AnalysisJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [populating, setPopulating] = useState(false);
  const [error, setError] = useState('');
  const [success, setSuccess] = useState('');
  const [activeTab, setActiveTab] = useState(0);
  const [dirty, setDirty] = useState(false);
  const [analysisData, setAnalysisData] = useState<AnalysisDataMap>({});

  // Source addition form
  const [addSourceConn, setAddSourceConn] = useState('');
  const [addSourcePrefix, setAddSourcePrefix] = useState('');

  const loadPlan = useCallback(async () => {
    if (!id) return;
    try {
      const result = await api.getPlan(id) as MigrationPlan;
      setPlan(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load plan');
    } finally {
      setLoading(false);
    }
  }, [id]);

  const loadConnections = useCallback(async () => {
    try {
      const conns = await api.listConnections() as Connection[];
      setConnections(conns);
    } catch { /* ignore */ }
  }, []);

  const loadAnalysisJobs = useCallback(async () => {
    try {
      const jobs = await api.listJobs() as AnalysisJob[];
      setAnalysisJobs(jobs.filter(j => j.status === 'completed' && j.name?.toLowerCase().includes('analysis')));
    } catch { /* ignore */ }
  }, []);

  const loadAnalysisData = useCallback(async () => {
    if (!plan) return;
    const data: AnalysisDataMap = {};
    for (const src of plan.sources) {
      if (!src.analysis_job_id) continue;
      if (data[src.analysis_job_id]) continue;
      try {
        const result = await api.getAnalysisResult(src.analysis_job_id) as Record<string, unknown>;
        const inner = (result.result ?? result) as Record<string, unknown>;
        if (inner.organizations) {
          data[src.analysis_job_id] = inner as AnalysisDataMap[string];
        }
      } catch { /* ignore */ }
    }
    setAnalysisData(data);
  }, [plan]);

  useEffect(() => { loadPlan(); loadConnections(); loadAnalysisJobs(); }, [loadPlan, loadConnections, loadAnalysisJobs]);
  useEffect(() => { loadAnalysisData(); }, [loadAnalysisData]);

  const sourceNames: Record<string, string> = {};
  if (plan) {
    for (const src of plan.sources) {
      const conn = connections.find(c => c.id === src.connection_id);
      sourceNames[src.id] = conn ? `${conn.name}${src.name_prefix ? ` [${src.name_prefix}]` : ''}` : src.connection_id;
    }
  }

  const handleSave = async () => {
    if (!plan || !id) return;
    setSaving(true);
    setError('');
    setSuccess('');
    try {
      await api.updatePlanPhases(id, {
        sources: plan.sources.map(s => ({
          id: s.id,
          connection_id: s.connection_id,
          name_prefix: s.name_prefix,
          analysis_job_id: s.analysis_job_id,
        })),
        phases: plan.phases.map(p => ({
          id: p.id.startsWith('new-') ? undefined : p.id,
          phase_number: p.phase_number,
          name: p.name,
          orgs: p.orgs.map(o => ({
            source_id: o.source_id,
            org_id: o.org_id,
            org_name: o.org_name,
          })),
        })),
      });
      setDirty(false);
      setSuccess('Plan saved successfully');
      loadPlan();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save');
    } finally {
      setSaving(false);
    }
  };

  const handlePopulate = async () => {
    if (!id || !plan) return;
    const sourcesWithoutAnalysis = plan.sources.filter(s => !s.analysis_job_id);
    if (sourcesWithoutAnalysis.length > 0) {
      setError('All sources must have an analysis scan selected before generating a plan.');
      return;
    }
    setPopulating(true);
    setError('');
    setSuccess('');
    try {
      // Save first to persist analysis_job_id assignments
      await api.updatePlanPhases(id, {
        sources: plan.sources.map(s => ({
          id: s.id,
          connection_id: s.connection_id,
          name_prefix: s.name_prefix,
          analysis_job_id: s.analysis_job_id,
        })),
        phases: plan.phases.map(p => ({
          id: p.id.startsWith('new-') ? undefined : p.id,
          phase_number: p.phase_number,
          name: p.name,
          orgs: p.orgs.map(o => ({
            source_id: o.source_id,
            org_id: o.org_id,
            org_name: o.org_name,
          })),
        })),
      });
      const result = await api.populatePlan(id) as MigrationPlan;
      setPlan(result);
      setDirty(false);
      setSuccess('Plan generated from analysis results. Switch to the Phases tab to review and modify.');
      setActiveTab(1);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to generate plan');
    } finally {
      setPopulating(false);
    }
  };

  const handleAddSource = () => {
    if (!plan || !addSourceConn) return;
    const newSource: PlanSource = {
      id: `new-${Date.now()}`,
      connection_id: addSourceConn,
      name_prefix: addSourcePrefix.trim() || null,
      analysis_job_id: null,
    };
    setPlan({ ...plan, sources: [...plan.sources, newSource] });
    setAddSourceConn('');
    setAddSourcePrefix('');
    setDirty(true);
  };

  const handleRemoveSource = (sourceId: string) => {
    if (!plan) return;
    setPlan({
      ...plan,
      sources: plan.sources.filter(s => s.id !== sourceId),
      phases: plan.phases.map(p => ({
        ...p,
        orgs: p.orgs.filter(o => o.source_id !== sourceId),
      })),
    });
    setDirty(true);
  };

  const handleSetAnalysisJob = (sourceId: string, jobId: string) => {
    if (!plan) return;
    setPlan({
      ...plan,
      sources: plan.sources.map(s =>
        s.id === sourceId ? { ...s, analysis_job_id: jobId || null } : s
      ),
    });
    setDirty(true);
  };

  const handlePhaseChange = (phases: PlanPhase[]) => {
    if (!plan) return;
    setPlan({ ...plan, phases });
    setDirty(true);
  };

  const handleExecutePhase = async (phaseId: string) => {
    if (!id) return;
    setError('');
    try {
      const res = await api.executePlanPhase(id, phaseId);
      setSuccess(`Phase execution started. Job ID: ${res.job_id}`);
      navigate(`/jobs/${res.job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to execute phase');
    }
  };

  if (loading) return <Spinner />;
  if (!plan) return <Alert variant="danger" isInline title={error || 'Plan not found'} />;

  const usedConnIds = plan.sources.map(s => s.connection_id);
  const availableSources = connections.filter(c => c.role === 'source' && !usedConnIds.includes(c.id));

  // Get analysis jobs relevant to a specific connection
  const getJobsForConnection = (connId: string) => {
    const conn = connections.find(c => c.id === connId);
    if (!conn) return analysisJobs;
    return analysisJobs.filter(j =>
      j.name?.includes(conn.url) || j.name?.includes(conn.name)
    );
  };

  const allSourcesHaveAnalysis = plan.sources.length > 0 && plan.sources.every(s => s.analysis_job_id);

  return (
    <>
      <Split hasGutter style={{ marginBottom: 16 }}>
        <SplitItem isFilled>
          <Title headingLevel="h1" size="2xl">{plan.name}</Title>
          {plan.description && (
            <TextContent><Text>{plan.description}</Text></TextContent>
          )}
        </SplitItem>
        <SplitItem>
          <Label color={plan.status === 'draft' ? 'blue' : plan.status === 'active' ? 'orange' : plan.status === 'completed' ? 'green' : 'red'}>
            {plan.status}
          </Label>
        </SplitItem>
        <SplitItem>
          <Button variant="secondary" onClick={() => navigate('/planner')}>Back to Plans</Button>
        </SplitItem>
      </Split>

      {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 12 }} />}
      {success && <Alert variant="success" isInline title={success} style={{ marginBottom: 12 }} />}

      <Tabs activeKey={activeTab} onSelect={(_e, k) => setActiveTab(k as number)} style={{ marginBottom: 16 }}>
        <Tab eventKey={0} title={<TabTitleText>Sources &amp; Scans</TabTitleText>}>
          <div style={{ padding: 16 }}>
            <Card style={{ marginBottom: 16 }}>
              <CardTitle>Source Connections</CardTitle>
              <CardBody>
                <TextContent style={{ marginBottom: 12 }}>
                  <Text component="small">
                    Add source connections and select the completed analysis scan to use for each.
                    Run scans from the Dependency Analysis page first.
                  </Text>
                </TextContent>

                {plan.sources.length === 0 ? (
                  <Alert variant="info" isInline title="No sources added yet. Add a source connection below." />
                ) : (
                  <Table variant="compact">
                    <Thead>
                      <Tr>
                        <Th>Connection</Th>
                        <Th>Name Prefix</Th>
                        <Th>Analysis Scan</Th>
                        <Th />
                      </Tr>
                    </Thead>
                    <Tbody>
                      {plan.sources.map(src => {
                        const conn = connections.find(c => c.id === src.connection_id);
                        const relevantJobs = getJobsForConnection(src.connection_id);
                        return (
                          <Tr key={src.id}>
                            <Td>{conn ? `${conn.name} (${conn.url})` : src.connection_id}</Td>
                            <Td>{src.name_prefix || '—'}</Td>
                            <Td>
                              <FormSelect
                                value={src.analysis_job_id || ''}
                                onChange={(_e, v) => handleSetAnalysisJob(src.id, v)}
                                style={{ minWidth: 300 }}
                              >
                                <FormSelectOption value="" label="-- Select completed scan --" isDisabled />
                                {(relevantJobs.length > 0 ? relevantJobs : analysisJobs).map(j => (
                                  <FormSelectOption
                                    key={j.id}
                                    value={j.id}
                                    label={`#${j.seq_id ?? '?'} — ${j.name} (${new Date(j.started_at).toLocaleString()})`}
                                  />
                                ))}
                              </FormSelect>
                            </Td>
                            <Td>
                              <Button variant="plain" size="sm" onClick={() => handleRemoveSource(src.id)} aria-label="Remove">
                                <TimesIcon />
                              </Button>
                            </Td>
                          </Tr>
                        );
                      })}
                    </Tbody>
                  </Table>
                )}

                <Split hasGutter style={{ marginTop: 16 }}>
                  <SplitItem>
                    <FormSelect value={addSourceConn} onChange={(_e, v) => setAddSourceConn(v)} style={{ width: 250 }}>
                      <FormSelectOption value="" label="-- Add source --" isDisabled />
                      {availableSources.map(c => (
                        <FormSelectOption key={c.id} value={c.id} label={`${c.name} (${c.url})`} />
                      ))}
                    </FormSelect>
                  </SplitItem>
                  <SplitItem>
                    <TextInput value={addSourcePrefix} onChange={(_e, v) => setAddSourcePrefix(v)} placeholder="Name prefix (optional)" style={{ width: 180 }} />
                  </SplitItem>
                  <SplitItem>
                    <Button variant="secondary" icon={<PlusCircleIcon />} onClick={handleAddSource} isDisabled={!addSourceConn}>
                      Add Source
                    </Button>
                  </SplitItem>
                </Split>
              </CardBody>
            </Card>

            <Card>
              <CardBody>
                <Split hasGutter>
                  <SplitItem isFilled>
                    <TextContent>
                      <Text component="small">
                        Once all sources have a scan selected, click &quot;Generate Plan&quot; to auto-populate migration phases based on dependency analysis.
                      </Text>
                    </TextContent>
                  </SplitItem>
                  <SplitItem>
                    <Button
                      variant="primary"
                      onClick={handlePopulate}
                      isDisabled={!allSourcesHaveAnalysis || populating || plan.sources.length === 0}
                      isLoading={populating}
                    >
                      Generate Plan
                    </Button>
                  </SplitItem>
                </Split>
              </CardBody>
            </Card>
          </div>
        </Tab>

        <Tab eventKey={1} title={<TabTitleText>Phases ({plan.phases.length})</TabTitleText>}>
          <div style={{ padding: 16 }}>
            {plan.phases.length === 0 ? (
              <Alert variant="info" isInline title="No phases yet. Go to Sources & Scans tab, select analysis scans, and click Generate Plan." />
            ) : (
              <>
                <Split hasGutter style={{ marginBottom: 16 }}>
                  <SplitItem isFilled>
                    <TextContent>
                      <Text component="small">Drag organizations between phases or add/remove phases to customize the migration order.</Text>
                    </TextContent>
                  </SplitItem>
                  <SplitItem>
                    <Button variant="primary" onClick={handleSave} isDisabled={!dirty || saving} isLoading={saving}>
                      Save Plan
                    </Button>
                  </SplitItem>
                </Split>

                <PhaseEditor
                  phases={plan.phases}
                  sources={plan.sources}
                  sourceNames={sourceNames}
                  analysisData={analysisData}
                  onChange={handlePhaseChange}
                />
              </>
            )}
          </div>
        </Tab>

        <Tab eventKey={2} title={<TabTitleText>Execute</TabTitleText>}>
          <div style={{ padding: 16 }}>
            {dirty && (
              <Alert variant="warning" isInline title="You have unsaved changes. Save the plan before executing." style={{ marginBottom: 12 }} />
            )}
            {plan.phases.length === 0 ? (
              <Alert variant="info" isInline title="No phases to execute. Generate and configure phases first." />
            ) : (
              <Flex direction={{ default: 'column' }} spaceItems={{ default: 'spaceItemsMd' }}>
                {plan.phases.map(phase => (
                  <FlexItem key={phase.id}>
                    <Card>
                      <CardBody>
                        <Split hasGutter>
                          <SplitItem isFilled>
                            <strong>{phase.name || `Phase ${phase.phase_number}`}</strong>
                            {' — '}
                            {phase.orgs.length} org(s)
                            {phase.job_id && (
                              <Button variant="link" size="sm" onClick={() => navigate(`/jobs/${phase.job_id}`)} style={{ marginLeft: 8 }}>
                                View Job
                              </Button>
                            )}
                          </SplitItem>
                          <SplitItem>
                            <Label
                              color={phase.status === 'completed' ? 'green' : phase.status === 'completed_with_errors' ? 'orange' : phase.status === 'running' ? 'orange' : phase.status === 'failed' ? 'red' : 'grey'}
                              isCompact
                            >
                              {phase.status}
                            </Label>
                          </SplitItem>
                          <SplitItem>
                            <Button
                              variant="primary"
                              size="sm"
                              onClick={() => handleExecutePhase(phase.id)}
                              isDisabled={dirty || phase.status === 'running' || phase.orgs.length === 0}
                            >
                              {phase.status === 'completed' ? 'Re-run' : 'Execute'}
                            </Button>
                          </SplitItem>
                        </Split>
                      </CardBody>
                    </Card>
                  </FlexItem>
                ))}
              </Flex>
            )}
          </div>
        </Tab>
      </Tabs>
    </>
  );
}
