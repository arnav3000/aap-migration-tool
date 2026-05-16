import { useState } from 'react';
import {
  Button,
  Card,
  CardBody,
  CardHeader,
  CardTitle,
  Text,
  Label,
  Split,
  SplitItem,
  Flex,
  FlexItem,
  TextInput,
  Alert,
} from '@patternfly/react-core';
import AngleUpIcon from '@patternfly/react-icons/dist/esm/icons/angle-up-icon';
import AngleDownIcon from '@patternfly/react-icons/dist/esm/icons/angle-down-icon';
import PlusCircleIcon from '@patternfly/react-icons/dist/esm/icons/plus-circle-icon';
import TimesIcon from '@patternfly/react-icons/dist/esm/icons/times-icon';
import ExclamationTriangleIcon from '@patternfly/react-icons/dist/esm/icons/exclamation-triangle-icon';
import type { PlanPhase, PlanPhaseOrg, PlanSource } from '../types/resources';

interface AnalysisOrgData {
  org_id: number;
  required_migrations_before: string[];
  can_migrate_standalone: boolean;
}

interface Props {
  phases: PlanPhase[];
  sources: PlanSource[];
  sourceNames: Record<string, string>;
  analysisData?: Record<string, { organizations: Record<string, AnalysisOrgData> }>;
  onChange: (phases: PlanPhase[]) => void;
}

export function PhaseEditor({ phases, sources, sourceNames, analysisData, onChange }: Props) {
  const [editingName, setEditingName] = useState<string | null>(null);

  const getOrgDependencyWarning = (org: PlanPhaseOrg, phaseNumber: number): string | null => {
    if (!analysisData) return null;
    for (const source of sources) {
      if (source.id !== org.source_id) continue;
      const data = source.analysis_job_id ? analysisData[source.analysis_job_id] : null;
      if (!data?.organizations) continue;
      const orgInfo = data.organizations[org.org_name];
      if (!orgInfo) continue;
      const deps = orgInfo.required_migrations_before || [];
      for (const depName of deps) {
        const depInLaterPhase = phases.some(
          p => p.phase_number > phaseNumber && p.orgs.some(o => o.org_name === depName)
        );
        if (depInLaterPhase) {
          return `Depends on "${depName}" which is in a later phase`;
        }
        const depNotInPlan = !phases.some(p => p.orgs.some(o => o.org_name === depName));
        if (depNotInPlan) {
          return `Depends on "${depName}" which is not in the plan`;
        }
      }
    }
    return null;
  };

  const moveOrg = (fromPhaseId: string, orgId: string, direction: 'up' | 'down') => {
    const fromIdx = phases.findIndex(p => p.id === fromPhaseId);
    const toIdx = direction === 'up' ? fromIdx - 1 : fromIdx + 1;
    if (toIdx < 0 || toIdx >= phases.length) return;

    const updated = phases.map(p => ({ ...p, orgs: [...p.orgs] }));
    const orgIdx = updated[fromIdx].orgs.findIndex(o => o.id === orgId);
    if (orgIdx === -1) return;

    const [org] = updated[fromIdx].orgs.splice(orgIdx, 1);
    updated[toIdx].orgs.push(org);
    onChange(updated);
  };

  const removeOrg = (phaseId: string, orgId: string) => {
    const updated = phases.map(p => {
      if (p.id !== phaseId) return p;
      return { ...p, orgs: p.orgs.filter(o => o.id !== orgId) };
    });
    onChange(updated);
  };

  const addPhase = () => {
    const maxNum = phases.reduce((max, p) => Math.max(max, p.phase_number), 0);
    const newPhase: PlanPhase = {
      id: `new-${Date.now()}`,
      phase_number: maxNum + 1,
      name: `Phase ${maxNum + 1}`,
      status: 'pending',
      orgs: [],
    };
    onChange([...phases, newPhase]);
  };

  const removePhase = (phaseId: string) => {
    const phase = phases.find(p => p.id === phaseId);
    if (!phase || phase.orgs.length > 0) return;
    const updated = phases
      .filter(p => p.id !== phaseId)
      .map((p, i) => ({ ...p, phase_number: i + 1 }));
    onChange(updated);
  };

  const renamePhase = (phaseId: string, name: string) => {
    const updated = phases.map(p => p.id === phaseId ? { ...p, name } : p);
    onChange(updated);
    setEditingName(null);
  };

  const statusColor = (status: string) => {
    switch (status) {
      case 'completed': return 'green';
      case 'running': return 'orange';
      case 'failed': return 'red';
      default: return 'grey';
    }
  };

  return (
    <div>
      {phases.length === 0 && (
        <Alert variant="info" isInline title="No phases yet. Use 'Auto-populate from Analysis' or add phases manually." style={{ marginBottom: 12 }} />
      )}

      {phases.map((phase) => (
        <Card key={phase.id} style={{ marginBottom: 12 }}>
          <CardHeader>
            <CardTitle>
              <Split hasGutter>
                <SplitItem>
                  {editingName === phase.id ? (
                    <TextInput
                      value={phase.name}
                      onChange={(_e, v) => {
                        const updated = phases.map(p => p.id === phase.id ? { ...p, name: v } : p);
                        onChange(updated);
                      }}
                      onBlur={() => setEditingName(null)}
                      onKeyDown={(e) => { if (e.key === 'Enter') setEditingName(null); }}
                      style={{ width: 200 }}
                      autoFocus
                    />
                  ) : (
                    <Button variant="plain" onClick={() => setEditingName(phase.id)}>
                      <strong>{phase.name || `Phase ${phase.phase_number}`}</strong>
                    </Button>
                  )}
                </SplitItem>
                <SplitItem>
                  <Label color={statusColor(phase.status)} isCompact>{phase.status}</Label>
                </SplitItem>
                <SplitItem>
                  <Label isCompact>{phase.orgs.length} org(s)</Label>
                </SplitItem>
                {phase.orgs.length === 0 && phase.status === 'pending' && (
                  <SplitItem>
                    <Button variant="plain" aria-label="Remove phase" onClick={() => removePhase(phase.id)}>
                      <TimesIcon />
                    </Button>
                  </SplitItem>
                )}
              </Split>
            </CardTitle>
          </CardHeader>
          <CardBody>
            {phase.orgs.length === 0 ? (
              <Text component="small" style={{ color: '#6a6e73' }}>No organizations assigned to this phase.</Text>
            ) : (
              <Flex direction={{ default: 'column' }} spaceItems={{ default: 'spaceItemsXs' }}>
                {phase.orgs.map(org => {
                  const warning = getOrgDependencyWarning(org, phase.phase_number);
                  const srcName = sourceNames[org.source_id] || 'Unknown';
                  return (
                    <FlexItem key={org.id}>
                      <Split hasGutter>
                        <SplitItem>
                          <Flex spaceItems={{ default: 'spaceItemsXs' }}>
                            <FlexItem>
                              <Button
                                variant="plain"
                                size="sm"
                                isDisabled={phases[0]?.id === phase.id}
                                onClick={() => moveOrg(phase.id, org.id, 'up')}
                                aria-label="Move up"
                              >
                                <AngleUpIcon />
                              </Button>
                            </FlexItem>
                            <FlexItem>
                              <Button
                                variant="plain"
                                size="sm"
                                isDisabled={phases[phases.length - 1]?.id === phase.id}
                                onClick={() => moveOrg(phase.id, org.id, 'down')}
                                aria-label="Move down"
                              >
                                <AngleDownIcon />
                              </Button>
                            </FlexItem>
                          </Flex>
                        </SplitItem>
                        <SplitItem isFilled>
                          <strong>{org.org_name}</strong>
                          <Label color="blue" isCompact style={{ marginLeft: 8 }}>{srcName}</Label>
                          {warning && (
                            <Label color="orange" isCompact icon={<ExclamationTriangleIcon />} style={{ marginLeft: 4 }}>
                              {warning}
                            </Label>
                          )}
                        </SplitItem>
                        <SplitItem>
                          <Button variant="plain" size="sm" onClick={() => removeOrg(phase.id, org.id)} aria-label="Remove">
                            <TimesIcon />
                          </Button>
                        </SplitItem>
                      </Split>
                    </FlexItem>
                  );
                })}
              </Flex>
            )}
          </CardBody>
        </Card>
      ))}

      <Button variant="link" icon={<PlusCircleIcon />} onClick={addPhase}>
        Add Phase
      </Button>
    </div>
  );
}
