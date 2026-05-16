import { useState, useEffect } from 'react';
import {
  Title,
  TextContent,
  Text,
  Form,
  FormGroup,
  FormSection,
  TextInput,
  FormSelect,
  FormSelectOption,
  Button,
  Card,
  CardBody,
  CardTitle,
  Gallery,
  DescriptionList,
  DescriptionListGroup,
  DescriptionListTerm,
  DescriptionListDescription,
  Alert,
  ExpandableSection,
  Tabs,
  Tab,
  TabTitleText,
  Spinner,
  Label,
  Split,
  SplitItem,
  List,
  ListItem,
} from '@patternfly/react-core';
import { api } from '../api/client';

interface SizingResult {
  input: Record<string, unknown>;
  execution_nodes: Record<string, unknown>;
  controller: Record<string, unknown>;
  database: Record<string, unknown>;
  deployment: DeploymentRecommendation | null;
  automation_hub: Record<string, unknown> | null;
  gateway: Record<string, unknown> | null;
  eda: Record<string, unknown> | null;
  redis: Record<string, unknown> | null;
  warnings: string[];
  validation_warnings: string[];
}

interface DeploymentRecommendation {
  target: string;
  recommended_topology: string;
  growth_viable: boolean;
  doc_link: string;
  enterprise_reasons?: string[];
  growth_limitations?: string[];
  // OCP fields
  cluster_type?: string;
  node_spec?: Record<string, number>;
  total_nodes?: number;
  worker_nodes?: number;
  worker_spec?: Record<string, number>;
  external_db?: Record<string, unknown>;
  db_type?: string;
  redis?: string;
  hub_storage?: string;
  // Containerized fields
  vm_count?: number;
  vm_spec?: Record<string, number>;
  vm_layout?: { purpose: string; count: number }[];
  layout?: string;
  db_storage_recommended_gb?: number;
}

interface DynamicSizingResult {
  mode: string;
  deployment_target: string;
  source_observed: Record<string, unknown>;
  derived_inputs: Record<string, unknown>;
  headroom_multiplier: number;
  recommendation: {
    deployment: DeploymentRecommendation;
    components: Record<string, Record<string, unknown>>;
    summary: Record<string, unknown>;
    warnings: string[];
    deployment_notes: string[];
  };
}

interface ConnectionItem {
  id: string;
  name: string;
  url: string;
  role: string;
  type: string;
  ping_status: string;
  auth_status: string;
}

export function Sizing() {
  const [activeTab, setActiveTab] = useState<number>(0);

  // === Shared State ===
  const [deploymentTarget, setDeploymentTarget] = useState('ocp');

  // === Manual Mode State ===
  const [managedHosts, setManagedHosts] = useState('5000');
  const [playbooksPerDay, setPlaybooksPerDay] = useState('100');
  const [jobDuration, setJobDuration] = useState('0.5');
  const [tasksPerJob, setTasksPerJob] = useState('50');
  const [forks, setForks] = useState('10');
  const [verbosity, setVerbosity] = useState('1');
  const [hoursPerDay, setHoursPerDay] = useState('8');
  const [peakPattern, setPeakPattern] = useState('business_hours');
  const [numControllers, setNumControllers] = useState('2');
  const [concurrentJobs, setConcurrentJobs] = useState('0');
  const [pendingJobs, setPendingJobs] = useState('0');
  const [jobRetention, setJobRetention] = useState('720');
  const [factRetention, setFactRetention] = useState('720');
  const [hubNodes, setHubNodes] = useState('1');
  const [edaNodes, setEdaNodes] = useState('0');

  const [result, setResult] = useState<SizingResult | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(false);

  // === Dynamic Mode State ===
  const [connections, setConnections] = useState<ConnectionItem[]>([]);
  const [selectedConnection, setSelectedConnection] = useState('');
  const [historyDays, setHistoryDays] = useState('30');
  const [dynamicResult, setDynamicResult] = useState<DynamicSizingResult | null>(null);
  const [dynamicError, setDynamicError] = useState<string | null>(null);
  const [dynamicLoading, setDynamicLoading] = useState(false);
  const [connectionsLoading, setConnectionsLoading] = useState(false);

  useEffect(() => {
    loadConnections();
  }, []);

  const loadConnections = async () => {
    setConnectionsLoading(true);
    try {
      const conns = await api.listConnections() as ConnectionItem[];
      setConnections(conns);
      if (conns.length > 0 && !selectedConnection) {
        const source = conns.find(c => c.role === 'source');
        setSelectedConnection(source?.id || conns[0].id);
      }
    } catch {
      // connections may not be configured
    } finally {
      setConnectionsLoading(false);
    }
  };

  const handleCalculate = async () => {
    setError(null);
    setLoading(true);
    try {
      const res = await api.calculateSizing({
        managed_hosts: parseInt(managedHosts),
        playbooks_per_day_peak: parseInt(playbooksPerDay),
        job_duration_hours: parseFloat(jobDuration),
        tasks_per_job: parseInt(tasksPerJob),
        forks_observed: parseInt(forks),
        verbosity_level: parseInt(verbosity),
        allowed_hours_per_day: parseFloat(hoursPerDay),
        peak_pattern: peakPattern,
        deployment_target: deploymentTarget,
        num_controllers: parseInt(numControllers),
        concurrent_jobs: parseInt(concurrentJobs),
        pending_jobs: parseInt(pendingJobs),
        job_retention_hours: parseInt(jobRetention),
        fact_retention_hours: parseInt(factRetention),
        hub_nodes: parseInt(hubNodes),
        eda_nodes: parseInt(edaNodes),
      });
      setResult(res as SizingResult);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Calculation failed');
    } finally {
      setLoading(false);
    }
  };

  const handleDynamicCalculate = async () => {
    if (!selectedConnection) {
      setDynamicError('Please select a connection');
      return;
    }
    setDynamicError(null);
    setDynamicResult(null);
    setDynamicLoading(true);
    try {
      const res = await api.calculateDynamicSizing(selectedConnection, parseInt(historyDays), deploymentTarget);
      setDynamicResult(res as DynamicSizingResult);
    } catch (err) {
      setDynamicError(err instanceof Error ? err.message : 'Dynamic sizing failed');
    } finally {
      setDynamicLoading(false);
    }
  };

  const fmtVal = (val: unknown): string => {
    if (val === null || val === undefined) return 'N/A';
    if (typeof val === 'object') return JSON.stringify(val);
    if (typeof val === 'number') return Number.isInteger(val) ? val.toLocaleString() : val.toFixed(2);
    let str = String(val);
    if (deploymentTarget === 'containerized') {
      str = str.replace(/\bpods\b/gi, 'instances').replace(/\bpod\b/gi, 'instance');
    }
    return str;
  };

  const podTerminologyMap: Record<string, string> = {
    'execution_pods': 'Execution Instances',
    'control_plane_pods': 'Control Plane Instances',
    'hub_pods': 'Hub Instances',
    'gateway_pods': 'Gateway Instances',
    'eda_pods': 'EDA Instances',
    'cpu_per_pod': 'CPU Per Instance',
    'memory_per_pod_gb': 'Memory Per Instance (GB)',
    'estimated_pods': 'Total Service Instances',
    'total_nodes': 'Redis Instances',
  };

  const fmtKey = (key: string) => {
    if (deploymentTarget === 'containerized' && podTerminologyMap[key]) {
      return podTerminologyMap[key];
    }
    return key.replace(/_/g, ' ').replace(/\b\w/g, c => c.toUpperCase());
  };

  const renderResultCard = (title: string, data: Record<string, unknown> | null, color?: string) => {
    if (!data) return null;
    const filtered = Object.entries(data).filter(([, val]) => typeof val !== 'object' || val === null);
    return (
      <Card style={{ borderTop: color ? `3px solid ${color}` : undefined }}>
        <CardTitle>{title}</CardTitle>
        <CardBody>
          <DescriptionList isHorizontal isCompact>
            {filtered.map(([key, val]) => (
              <DescriptionListGroup key={key}>
                <DescriptionListTerm>{fmtKey(key)}</DescriptionListTerm>
                <DescriptionListDescription>{fmtVal(val)}</DescriptionListDescription>
              </DescriptionListGroup>
            ))}
          </DescriptionList>
        </CardBody>
      </Card>
    );
  };

  const renderDeploymentCard = (deployment: DeploymentRecommendation | null | undefined) => {
    if (!deployment) return null;
    const isEnterprise = deployment.recommended_topology === 'enterprise';
    const targetLabel = deployment.target === 'ocp' ? 'OpenShift (Operator)' : 'Containerized (Podman)';

    return (
      <Card style={{ marginBottom: 16, borderTop: `3px solid ${isEnterprise ? '#c9190b' : '#3e8635'}` }}>
        <CardTitle>
          <Split hasGutter>
            <SplitItem>Topology Recommendation</SplitItem>
            <SplitItem>
              <Label color={isEnterprise ? 'red' : 'green'} isCompact>
                {deployment.recommended_topology.toUpperCase()}
              </Label>
              <Label color="blue" isCompact style={{ marginLeft: 8 }}>
                {targetLabel}
              </Label>
            </SplitItem>
          </Split>
        </CardTitle>
        <CardBody>
          <DescriptionList isHorizontal isCompact>
            {deployment.cluster_type && (
              <DescriptionListGroup>
                <DescriptionListTerm>Cluster Type</DescriptionListTerm>
                <DescriptionListDescription>{deployment.cluster_type}</DescriptionListDescription>
              </DescriptionListGroup>
            )}
            {deployment.worker_nodes && (
              <DescriptionListGroup>
                <DescriptionListTerm>Worker Nodes</DescriptionListTerm>
                <DescriptionListDescription>{deployment.worker_nodes}</DescriptionListDescription>
              </DescriptionListGroup>
            )}
            {deployment.worker_spec && (
              <DescriptionListGroup>
                <DescriptionListTerm>Per Worker</DescriptionListTerm>
                <DescriptionListDescription>
                  {deployment.worker_spec.cpu} CPU / {deployment.worker_spec.memory_gb} GB RAM / {deployment.worker_spec.disk_gb} GB disk
                </DescriptionListDescription>
              </DescriptionListGroup>
            )}
            {deployment.total_nodes !== undefined && (
              <DescriptionListGroup>
                <DescriptionListTerm>Total Nodes</DescriptionListTerm>
                <DescriptionListDescription>{deployment.total_nodes}</DescriptionListDescription>
              </DescriptionListGroup>
            )}
            {deployment.node_spec && (
              <DescriptionListGroup>
                <DescriptionListTerm>Node Spec (SNO)</DescriptionListTerm>
                <DescriptionListDescription>
                  {deployment.node_spec.cpu} CPU / {deployment.node_spec.memory_gb} GB RAM / {deployment.node_spec.disk_gb} GB disk
                </DescriptionListDescription>
              </DescriptionListGroup>
            )}
            {deployment.vm_count !== undefined && (
              <DescriptionListGroup>
                <DescriptionListTerm>Total VMs</DescriptionListTerm>
                <DescriptionListDescription>{deployment.vm_count}</DescriptionListDescription>
              </DescriptionListGroup>
            )}
            {deployment.vm_spec && (
              <DescriptionListGroup>
                <DescriptionListTerm>Per VM</DescriptionListTerm>
                <DescriptionListDescription>
                  {deployment.vm_spec.cpu} CPU / {deployment.vm_spec.memory_gb} GB RAM / {deployment.vm_spec.disk_gb} GB disk
                </DescriptionListDescription>
              </DescriptionListGroup>
            )}
            {deployment.redis && (
              <DescriptionListGroup>
                <DescriptionListTerm>Redis</DescriptionListTerm>
                <DescriptionListDescription>{deployment.redis}</DescriptionListDescription>
              </DescriptionListGroup>
            )}
            {deployment.hub_storage && (
              <DescriptionListGroup>
                <DescriptionListTerm>Hub Storage</DescriptionListTerm>
                <DescriptionListDescription>{deployment.hub_storage}</DescriptionListDescription>
              </DescriptionListGroup>
            )}
            {deployment.db_type && (
              <DescriptionListGroup>
                <DescriptionListTerm>Database</DescriptionListTerm>
                <DescriptionListDescription>{deployment.db_type}</DescriptionListDescription>
              </DescriptionListGroup>
            )}
          </DescriptionList>

          {deployment.external_db && (
            <div style={{ marginTop: 12 }}>
              <Text component="small"><strong>External Database Requirements:</strong></Text>
              <DescriptionList isHorizontal isCompact style={{ marginTop: 4 }}>
                {Object.entries(deployment.external_db).filter(([,v]) => typeof v !== 'object').map(([k, v]) => (
                  <DescriptionListGroup key={k}>
                    <DescriptionListTerm>{fmtKey(k)}</DescriptionListTerm>
                    <DescriptionListDescription>{fmtVal(v)}</DescriptionListDescription>
                  </DescriptionListGroup>
                ))}
              </DescriptionList>
            </div>
          )}

          {deployment.vm_layout && (
            <div style={{ marginTop: 12 }}>
              <Text component="small"><strong>VM Layout:</strong></Text>
              <List isPlain style={{ marginTop: 4 }}>
                {deployment.vm_layout.map((item, i) => (
                  <ListItem key={i}>{item.count}x {item.purpose}</ListItem>
                ))}
              </List>
            </div>
          )}

          {deployment.layout && (
            <div style={{ marginTop: 12 }}>
              <Text component="small"><strong>Layout:</strong> {deployment.layout}</Text>
            </div>
          )}

          {isEnterprise && deployment.enterprise_reasons && (
            <Alert variant="info" isInline isPlain title="Why enterprise topology" style={{ marginTop: 12 }}>
              <List isPlain>
                {deployment.enterprise_reasons.map((r, i) => <ListItem key={i}>{r}</ListItem>)}
              </List>
            </Alert>
          )}

          {!isEnterprise && deployment.growth_limitations && (
            <Alert variant="warning" isInline isPlain title="Growth topology limitations" style={{ marginTop: 12 }}>
              <List isPlain>
                {deployment.growth_limitations.map((l, i) => <ListItem key={i}>{l}</ListItem>)}
              </List>
            </Alert>
          )}

          <div style={{ marginTop: 12 }}>
            <a href={deployment.doc_link} target="_blank" rel="noopener noreferrer">
              Red Hat Tested Topology Documentation
            </a>
          </div>
        </CardBody>
      </Card>
    );
  };

  const renderDeploymentTargetSelector = (id: string) => (
    <FormGroup label="Deployment Target" isRequired fieldId={id}>
      <FormSelect id={id} value={deploymentTarget} onChange={(_e, v) => setDeploymentTarget(v)}>
        <FormSelectOption value="ocp" label="OpenShift Container Platform (Operator)" />
        <FormSelectOption value="containerized" label="Containerized (Podman on RHEL)" />
      </FormSelect>
    </FormGroup>
  );

  const renderManualTab = () => (
    <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', marginTop: 16 }}>
      <Card style={{ flex: '0 0 420px' }}>
        <CardTitle>Input Parameters</CardTitle>
        <CardBody>
          <Form>
            <FormSection title="Deployment">
              {renderDeploymentTargetSelector('manual-target')}
            </FormSection>
            <FormSection title="Workload">
              <FormGroup label="Managed Hosts" isRequired fieldId="hosts">
                <TextInput id="hosts" type="number" value={managedHosts} onChange={(_e, v) => setManagedHosts(v)} />
              </FormGroup>
              <FormGroup label="Playbooks / Day (Peak)" isRequired fieldId="ppd">
                <TextInput id="ppd" type="number" value={playbooksPerDay} onChange={(_e, v) => setPlaybooksPerDay(v)} />
              </FormGroup>
              <FormGroup label="Avg Job Duration (hours)" fieldId="duration">
                <TextInput id="duration" type="number" value={jobDuration} onChange={(_e, v) => setJobDuration(v)} />
              </FormGroup>
              <FormGroup label="Tasks / Job" fieldId="tasks">
                <TextInput id="tasks" type="number" value={tasksPerJob} onChange={(_e, v) => setTasksPerJob(v)} />
              </FormGroup>
              <FormGroup label="Forks" fieldId="forks">
                <TextInput id="forks" type="number" value={forks} onChange={(_e, v) => setForks(v)} />
              </FormGroup>
              <FormGroup label="Verbosity Level" fieldId="verbosity">
                <FormSelect id="verbosity" value={verbosity} onChange={(_e, v) => setVerbosity(v)}>
                  <FormSelectOption value="0" label="0 - Minimal" />
                  <FormSelectOption value="1" label="1 - Normal" />
                  <FormSelectOption value="2" label="2 - Verbose" />
                  <FormSelectOption value="3" label="3 - More Verbose" />
                  <FormSelectOption value="4" label="4 - Debug" />
                </FormSelect>
              </FormGroup>
              <FormGroup label="Automation Hours / Day" fieldId="hours">
                <TextInput id="hours" type="number" value={hoursPerDay} onChange={(_e, v) => setHoursPerDay(v)} />
              </FormGroup>
              <FormGroup label="Peak Pattern" fieldId="pattern">
                <FormSelect id="pattern" value={peakPattern} onChange={(_e, v) => setPeakPattern(v)}>
                  <FormSelectOption value="distributed_24x7" label="Distributed 24x7" />
                  <FormSelectOption value="business_hours" label="Business Hours" />
                  <FormSelectOption value="batch_window" label="Batch Window" />
                  <FormSelectOption value="mixed" label="Mixed" />
                </FormSelect>
              </FormGroup>
            </FormSection>

            <ExpandableSection toggleText="Advanced Settings">
              <FormGroup label="Controller Nodes" fieldId="controllers">
                <TextInput id="controllers" type="number" value={numControllers} onChange={(_e, v) => setNumControllers(v)} />
              </FormGroup>
              <FormGroup label="Max Concurrent Jobs (0=auto)" fieldId="concurrent">
                <TextInput id="concurrent" type="number" value={concurrentJobs} onChange={(_e, v) => setConcurrentJobs(v)} />
              </FormGroup>
              <FormGroup label="Typical Pending Jobs" fieldId="pending">
                <TextInput id="pending" type="number" value={pendingJobs} onChange={(_e, v) => setPendingJobs(v)} />
              </FormGroup>
              <FormGroup label="Job Retention (hours)" fieldId="jobret">
                <TextInput id="jobret" type="number" value={jobRetention} onChange={(_e, v) => setJobRetention(v)} />
              </FormGroup>
              <FormGroup label="Fact Retention (hours)" fieldId="factret">
                <TextInput id="factret" type="number" value={factRetention} onChange={(_e, v) => setFactRetention(v)} />
              </FormGroup>
              <FormGroup label="Automation Hub Nodes (0=disabled)" fieldId="hub">
                <TextInput id="hub" type="number" value={hubNodes} onChange={(_e, v) => setHubNodes(v)} />
              </FormGroup>
              <FormGroup label="EDA Nodes (0=disabled)" fieldId="eda">
                <TextInput id="eda" type="number" value={edaNodes} onChange={(_e, v) => setEdaNodes(v)} />
              </FormGroup>
            </ExpandableSection>

            <Button variant="primary" onClick={handleCalculate} isLoading={loading} isDisabled={loading} style={{ marginTop: 16 }}>
              Calculate
            </Button>
          </Form>
        </CardBody>
      </Card>

      <div style={{ flex: 1, minWidth: 300 }}>
        {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 16 }} />}

        {result && result.warnings.length > 0 && (
          <Alert variant="warning" isInline title="Sizing Warnings" style={{ marginBottom: 16 }}>
            <ul>{result.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
          </Alert>
        )}

        {result && result.validation_warnings.length > 0 && (
          <Alert variant="info" isInline title="Validation Notes" style={{ marginBottom: 16 }}>
            <ul>{result.validation_warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
          </Alert>
        )}

        {result && renderDeploymentCard(result.deployment)}

        {result && (
          <Gallery hasGutter minWidths={{ default: '300px' }}>
            {renderResultCard('Execution Nodes', result.execution_nodes, '#0066cc')}
            {renderResultCard('Controller', result.controller, '#3e8635')}
            {renderResultCard('Database', result.database, '#f0ab00')}
            {renderResultCard('Automation Hub', result.automation_hub, '#6753ac')}
            {renderResultCard('Platform Gateway', result.gateway, '#009596')}
            {renderResultCard('Event-Driven Ansible', result.eda, '#c9190b')}
            {renderResultCard('Redis', result.redis, '#ec7a08')}
          </Gallery>
        )}
      </div>
    </div>
  );

  const renderDynamicTab = () => (
    <div style={{ display: 'flex', gap: 24, flexWrap: 'wrap', marginTop: 16 }}>
      <Card style={{ flex: '0 0 420px' }}>
        <CardTitle>Dynamic Sizing from Live AAP</CardTitle>
        <CardBody>
          <TextContent style={{ marginBottom: 16 }}>
            <Text component="small">
              Connect to your current AAP instance to automatically analyze job history, instances,
              and workload patterns. Sizing recommendations include 25% headroom and never go below
              AAP 2.6 minimum specs.
            </Text>
          </TextContent>

          {connectionsLoading ? (
            <Spinner size="md" />
          ) : connections.length === 0 ? (
            <Alert variant="info" isInline title="No connections configured" style={{ marginBottom: 16 }}>
              Add a source AAP connection first via the Connections page.
            </Alert>
          ) : (
            <Form>
              {renderDeploymentTargetSelector('dyn-target')}
              <FormGroup label="Source AAP Connection" isRequired fieldId="dyn-conn">
                <FormSelect
                  id="dyn-conn"
                  value={selectedConnection}
                  onChange={(_e, v) => setSelectedConnection(v)}
                >
                  {connections.map(c => (
                    <FormSelectOption
                      key={c.id}
                      value={c.id}
                      label={`${c.name} (${c.url})`}
                    />
                  ))}
                </FormSelect>
              </FormGroup>
              <FormGroup label="History Days (more = more accurate)" fieldId="dyn-days">
                <TextInput
                  id="dyn-days"
                  type="number"
                  value={historyDays}
                  onChange={(_e, v) => setHistoryDays(v)}
                />
              </FormGroup>

              <Button
                variant="primary"
                onClick={handleDynamicCalculate}
                isLoading={dynamicLoading}
                isDisabled={dynamicLoading || !selectedConnection}
                style={{ marginTop: 16 }}
              >
                {dynamicLoading ? 'Analyzing...' : 'Analyze & Calculate'}
              </Button>
            </Form>
          )}
        </CardBody>
      </Card>

      <div style={{ flex: 1, minWidth: 300 }}>
        {dynamicError && <Alert variant="danger" isInline title={dynamicError} style={{ marginBottom: 16 }} />}

        {dynamicResult && (
          <>
            {/* Observed Metrics */}
            <Card style={{ marginBottom: 16, borderTop: '3px solid #8a8d90' }}>
              <CardTitle>
                <Split hasGutter>
                  <SplitItem>Observed from Source AAP</SplitItem>
                  <SplitItem>
                    <Label color="blue" isCompact>
                      {String(dynamicResult.source_observed.jobs_analyzed || 0)} jobs analyzed over{' '}
                      {String(dynamicResult.source_observed.analysis_days || 0)} days
                    </Label>
                  </SplitItem>
                </Split>
              </CardTitle>
              <CardBody>
                <DescriptionList isHorizontal isCompact columnModifier={{ default: '2Col' }}>
                  <DescriptionListGroup>
                    <DescriptionListTerm>AAP Version</DescriptionListTerm>
                    <DescriptionListDescription>{fmtVal(dynamicResult.source_observed.version)}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Managed Hosts</DescriptionListTerm>
                    <DescriptionListDescription>{fmtVal(dynamicResult.source_observed.managed_hosts)}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Total Instances</DescriptionListTerm>
                    <DescriptionListDescription>{fmtVal(dynamicResult.source_observed.total_instances)}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Instance Groups</DescriptionListTerm>
                    <DescriptionListDescription>{fmtVal(dynamicResult.source_observed.instance_groups)}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Peak Playbooks/Day</DescriptionListTerm>
                    <DescriptionListDescription>{fmtVal(dynamicResult.source_observed.playbooks_per_day_peak)}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Avg Playbooks/Day</DescriptionListTerm>
                    <DescriptionListDescription>{fmtVal(dynamicResult.source_observed.playbooks_per_day_avg)}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Avg Job Duration</DescriptionListTerm>
                    <DescriptionListDescription>{fmtVal(dynamicResult.source_observed.job_duration_hours_avg)} hrs</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Detected Peak Pattern</DescriptionListTerm>
                    <DescriptionListDescription>
                      <Label color="purple" isCompact>{String(dynamicResult.source_observed.detected_peak_pattern)}</Label>
                    </DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Current Total CPU</DescriptionListTerm>
                    <DescriptionListDescription>{fmtVal(dynamicResult.source_observed.total_current_cpu)} cores</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Current Total Memory</DescriptionListTerm>
                    <DescriptionListDescription>{fmtVal(dynamicResult.source_observed.total_current_memory_gb)} GB</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Avg Forks Configured</DescriptionListTerm>
                    <DescriptionListDescription>{fmtVal(dynamicResult.source_observed.avg_forks_configured)}</DescriptionListDescription>
                  </DescriptionListGroup>
                  <DescriptionListGroup>
                    <DescriptionListTerm>Headroom Applied</DescriptionListTerm>
                    <DescriptionListDescription>{(dynamicResult.headroom_multiplier * 100 - 100).toFixed(0)}% buffer</DescriptionListDescription>
                  </DescriptionListGroup>
                </DescriptionList>
              </CardBody>
            </Card>

            {/* Topology Recommendation */}
            {renderDeploymentCard(dynamicResult.recommendation.deployment)}

            {/* Derived Inputs */}
            <ExpandableSection toggleText="Derived Calculator Inputs" style={{ marginBottom: 16 }}>
              <Card>
                <CardBody>
                  <DescriptionList isHorizontal isCompact>
                    {Object.entries(dynamicResult.derived_inputs)
                      .filter(([, v]) => typeof v !== 'object')
                      .map(([key, val]) => (
                      <DescriptionListGroup key={key}>
                        <DescriptionListTerm>{fmtKey(key)}</DescriptionListTerm>
                        <DescriptionListDescription>{fmtVal(val)}</DescriptionListDescription>
                      </DescriptionListGroup>
                    ))}
                  </DescriptionList>
                </CardBody>
              </Card>
            </ExpandableSection>

            {/* Warnings */}
            {dynamicResult.recommendation.warnings && dynamicResult.recommendation.warnings.length > 0 && (
              <Alert variant="warning" isInline title="Sizing Warnings" style={{ marginBottom: 16 }}>
                <ul>{dynamicResult.recommendation.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
              </Alert>
            )}

            {/* Recommendation Cards */}
            <Gallery hasGutter minWidths={{ default: '300px' }}>
              {renderResultCard('Execution Nodes', dynamicResult.recommendation.components?.automation_controller_execution_plane, '#0066cc')}
              {renderResultCard('Controller', dynamicResult.recommendation.components?.automation_controller_control_plane, '#3e8635')}
              {renderResultCard('Database', dynamicResult.recommendation.components?.database, '#f0ab00')}
              {renderResultCard('Automation Hub', dynamicResult.recommendation.components?.automation_hub, '#6753ac')}
              {renderResultCard('Platform Gateway', dynamicResult.recommendation.components?.platform_gateway, '#009596')}
              {renderResultCard('Event-Driven Ansible', dynamicResult.recommendation.components?.event_driven_ansible, '#c9190b')}
              {renderResultCard('Redis', dynamicResult.recommendation.components?.redis, '#ec7a08')}
            </Gallery>

            {/* Summary */}
            {dynamicResult.recommendation.summary && (
              <Card style={{ marginTop: 16, borderTop: '3px solid #151515' }}>
                <CardTitle>Total Resource Summary</CardTitle>
                <CardBody>
                  <DescriptionList isHorizontal isCompact>
                    {Object.entries(dynamicResult.recommendation.summary).map(([key, val]) => (
                      <DescriptionListGroup key={key}>
                        <DescriptionListTerm>{fmtKey(key)}</DescriptionListTerm>
                        <DescriptionListDescription>{fmtVal(val)}</DescriptionListDescription>
                      </DescriptionListGroup>
                    ))}
                  </DescriptionList>
                </CardBody>
              </Card>
            )}

            {/* Deployment Notes */}
            {dynamicResult.recommendation.deployment_notes && (
              <ExpandableSection toggleText="Deployment Notes" style={{ marginTop: 16 }}>
                <Card>
                  <CardBody>
                    <ul>
                      {dynamicResult.recommendation.deployment_notes.map((note, i) => (
                        <li key={i} style={{ marginBottom: 4 }}>{note}</li>
                      ))}
                    </ul>
                  </CardBody>
                </Card>
              </ExpandableSection>
            )}
          </>
        )}
      </div>
    </div>
  );

  return (
    <>
      <Title headingLevel="h1" size="2xl">Sizing Calculator</Title>
      <TextContent style={{ marginBottom: 16 }}>
        <Text>Calculate AAP 2.6 infrastructure sizing based on Red Hat official formulas and tested topologies.</Text>
      </TextContent>

      <Tabs activeKey={activeTab} onSelect={(_e, idx) => setActiveTab(idx as number)}>
        <Tab eventKey={0} title={<TabTitleText>Manual</TabTitleText>}>
          {renderManualTab()}
        </Tab>
        <Tab eventKey={1} title={<TabTitleText>Dynamic (from Live AAP)</TabTitleText>}>
          {renderDynamicTab()}
        </Tab>
      </Tabs>
    </>
  );
}
