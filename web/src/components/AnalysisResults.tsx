import { useState } from 'react';
import {
  Title,
  Text,
  Button,
  Card,
  CardBody,
  CardTitle,
  CardExpandableContent,
  Alert,
  Tabs,
  Tab,
  TabTitleText,
  Label,
  Split,
  SplitItem,
  ExpandableSection,
  DescriptionList,
  DescriptionListGroup,
  DescriptionListTerm,
  DescriptionListDescription,
} from '@patternfly/react-core';
import { Table, Thead, Tbody, Tr, Th, Td } from '@patternfly/react-table';

interface DuplicateDetail {
  name: string;
  resource_type: string;
  count: number;
  ids: number[];
  severity: string;
  impact: string;
  recommendation: string;
}

interface NamingPatternDetail {
  dominant_pattern: string;
  consistency_score: number;
  total_resources: number;
  case_style: Record<string, number>;
  prefixes: Record<string, number>;
  separators: Record<string, number>;
  violations: unknown[];
}

interface QualityData {
  quality_score: number;
  duplicate_count: number;
  duplicates: DuplicateDetail[];
  naming_pattern: NamingPatternDetail | null;
}

interface OrgData {
  org_id: number;
  resource_count: number;
  has_cross_org_deps: boolean;
  can_migrate_standalone: boolean;
  required_migrations_before: string[];
  blocks: string[];
  dependencies: Record<string, { resource_type: string; resource_id: number; resource_name: string; required_by: string[] }[]>;
  quality: QualityData | null;
  resources: Record<string, number>;
}

export interface AnalysisData {
  analysis_date: string;
  source_url: string;
  total_organizations: number;
  analyzed_organizations: string[];
  independent_orgs: string[];
  dependent_orgs: string[];
  migration_order: string[];
  migration_phases: { phase: number; orgs: string[]; description: string }[];
  organizations: Record<string, OrgData>;
  global_resources: Record<string, number>;
  total_duplicates: number;
  average_quality_score: number;
  circular_dependencies: string[][];
}

interface Props {
  data: AnalysisData;
}

export function AnalysisResults({ data }: Props) {
  const [activeTab, setActiveTab] = useState(0);
  const [expandedOrg, setExpandedOrg] = useState<string | null>(null);

  const severityColor = (s: string) => s === 'error' ? 'red' : s === 'warning' ? 'orange' : 'blue';
  const qualityColor = (score: number) => score >= 80 ? 'green' : score >= 50 ? 'orange' : 'red';

  return (
    <Tabs activeKey={activeTab} onSelect={(_e, k) => setActiveTab(k as number)}>
      <Tab eventKey={0} title={<TabTitleText>Summary</TabTitleText>}>
        <div style={{ padding: 16 }}>
          <Split hasGutter style={{ marginBottom: 16 }}>
            <SplitItem><Card><CardBody>
              <Title headingLevel="h3" size="2xl">{data.total_organizations}</Title>
              <Text>Total Orgs</Text>
            </CardBody></Card></SplitItem>
            <SplitItem><Card><CardBody>
              <Title headingLevel="h3" size="2xl">{data.independent_orgs.length}</Title>
              <Text>Independent</Text>
            </CardBody></Card></SplitItem>
            <SplitItem><Card><CardBody>
              <Title headingLevel="h3" size="2xl">{data.dependent_orgs.length}</Title>
              <Text>Have Dependencies</Text>
            </CardBody></Card></SplitItem>
            <SplitItem><Card><CardBody>
              <Title headingLevel="h3" size="2xl">{data.total_duplicates}</Title>
              <Text>Total Duplicates</Text>
            </CardBody></Card></SplitItem>
            <SplitItem><Card><CardBody>
              <Title headingLevel="h3" size="2xl">
                <Label color={qualityColor(data.average_quality_score)}>{data.average_quality_score.toFixed(0)}</Label>
              </Title>
              <Text>Avg Quality</Text>
            </CardBody></Card></SplitItem>
          </Split>

          {data.circular_dependencies && data.circular_dependencies.length > 0 && (
            <Alert variant="warning" isInline title="Circular Dependencies Detected" style={{ marginBottom: 16 }}>
              <Text component="p">
                The following organizations have mutual dependencies and must be migrated together
                (or dependencies broken before migration):
              </Text>
              {data.circular_dependencies.map((cycle, i) => (
                <div key={i} style={{ marginTop: 8 }}>
                  <strong>Cycle {i + 1}:</strong>{' '}
                  {cycle.map(org => <Label key={org} color="orange" isCompact style={{ margin: 1 }}>{org}</Label>)}
                </div>
              ))}
            </Alert>
          )}

          <Card style={{ marginBottom: 16 }}>
            <CardTitle>Migration Order</CardTitle>
            <CardBody>
              {data.migration_order.map((org, i) => (
                <Label key={org} color={data.independent_orgs.includes(org) ? 'green' : 'blue'} style={{ margin: 2 }}>
                  {i + 1}. {org}
                </Label>
              ))}
            </CardBody>
          </Card>

          {(() => {
            const blockers = Object.entries(data.organizations)
              .filter(([, o]) => o.blocks.length > 0)
              .sort(([, a], [, b]) => b.blocks.length - a.blocks.length);
            if (blockers.length === 0) return null;
            return (
              <Card style={{ marginBottom: 16 }}>
                <CardTitle>Migration Blockers (Critical Path)</CardTitle>
                <CardBody>
                  <Table variant="compact">
                    <Thead><Tr><Th>Organization</Th><Th>Blocks</Th><Th>Blocked Orgs</Th></Tr></Thead>
                    <Tbody>
                      {blockers.map(([name, org]) => (
                        <Tr key={name}>
                          <Td><Label color="red" isCompact>{name}</Label></Td>
                          <Td>{org.blocks.length} org(s)</Td>
                          <Td>{org.blocks.map(b => <Label key={b} color="grey" isCompact style={{ margin: 1 }}>{b}</Label>)}</Td>
                        </Tr>
                      ))}
                    </Tbody>
                  </Table>
                </CardBody>
              </Card>
            );
          })()}

          {data.global_resources && Object.keys(data.global_resources).length > 0 && (
            <Card style={{ marginBottom: 16 }}>
              <CardTitle>Global Resources (not org-scoped)</CardTitle>
              <CardBody>
                <Split hasGutter>
                  {Object.entries(data.global_resources).map(([type, count]) => (
                    <SplitItem key={type}>
                      <Label color="purple" isCompact>{type}: {count}</Label>
                    </SplitItem>
                  ))}
                </Split>
              </CardBody>
            </Card>
          )}
        </div>
      </Tab>

      <Tab eventKey={1} title={<TabTitleText>Migration Phases</TabTitleText>}>
        <div style={{ padding: 16 }}>
          {data.migration_phases.map((phase, i) => (
            <Card key={i} style={{ marginBottom: 8 }}>
              <CardTitle>Phase {phase.phase}</CardTitle>
              <CardBody>
                <Text component="p" style={{ marginBottom: 8 }}>{phase.description}</Text>
                {phase.orgs.map(org => (
                  <Label key={org} color="blue" style={{ margin: 2 }}>{org}</Label>
                ))}
              </CardBody>
            </Card>
          ))}
        </div>
      </Tab>

      <Tab eventKey={2} title={<TabTitleText>Organizations</TabTitleText>}>
        <div style={{ padding: 16 }}>
          {Object.entries(data.organizations).map(([name, org]) => (
            <Card key={name} style={{ marginBottom: 8 }} isExpanded={expandedOrg === name}>
              <CardTitle>
                <Split hasGutter>
                  <SplitItem>
                    <Button variant="plain" onClick={() => setExpandedOrg(expandedOrg === name ? null : name)}>
                      {expandedOrg === name ? '▼' : '▶'} {name}
                    </Button>
                  </SplitItem>
                  <SplitItem><Label isCompact>{org.resource_count} resources</Label></SplitItem>
                  <SplitItem>
                    <Label color={org.can_migrate_standalone ? 'green' : 'orange'} isCompact>
                      {org.can_migrate_standalone ? 'Standalone' : 'Has Dependencies'}
                    </Label>
                  </SplitItem>
                  {org.blocks.length > 0 && (
                    <SplitItem><Label color="red" isCompact>Blocks {org.blocks.length} org(s)</Label></SplitItem>
                  )}
                  {org.quality && (
                    <SplitItem>
                      <Label color={qualityColor(org.quality.quality_score)} isCompact>
                        Quality: {org.quality.quality_score.toFixed(0)}
                      </Label>
                    </SplitItem>
                  )}
                </Split>
              </CardTitle>
              <CardExpandableContent>
                <CardBody>
                  <DescriptionList isHorizontal isCompact>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Org ID</DescriptionListTerm>
                      <DescriptionListDescription>{org.org_id}</DescriptionListDescription>
                    </DescriptionListGroup>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Must Migrate After</DescriptionListTerm>
                      <DescriptionListDescription>
                        {org.required_migrations_before.length > 0
                          ? org.required_migrations_before.map(o => <Label key={o} color="blue" isCompact style={{ margin: 1 }}>{o}</Label>)
                          : 'None (independent)'}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Blocks</DescriptionListTerm>
                      <DescriptionListDescription>
                        {org.blocks.length > 0
                          ? org.blocks.map(o => <Label key={o} color="red" isCompact style={{ margin: 1 }}>{o}</Label>)
                          : 'None'}
                      </DescriptionListDescription>
                    </DescriptionListGroup>
                  </DescriptionList>

                  {Object.keys(org.resources).length > 0 && (
                    <ExpandableSection toggleText="Resource Breakdown" style={{ marginTop: 12 }}>
                      <Split hasGutter>
                        {Object.entries(org.resources).map(([type, count]) => (
                          <SplitItem key={type}><Label isCompact>{type}: {count}</Label></SplitItem>
                        ))}
                      </Split>
                    </ExpandableSection>
                  )}

                  {Object.keys(org.dependencies).length > 0 && (
                    <ExpandableSection toggleText={`Dependencies (${Object.keys(org.dependencies).length} org(s))`} style={{ marginTop: 12 }}>
                      {Object.entries(org.dependencies).map(([depOrg, deps]) => (
                        <div key={depOrg} style={{ marginBottom: 8 }}>
                          <Text component="p"><strong>From {depOrg}:</strong></Text>
                          <Table variant="compact">
                            <Thead><Tr><Th>Type</Th><Th>Name</Th><Th>ID</Th><Th>Used By</Th></Tr></Thead>
                            <Tbody>
                              {deps.map((d, i) => (
                                <Tr key={i}>
                                  <Td>{d.resource_type}</Td>
                                  <Td>{d.resource_name}</Td>
                                  <Td>{d.resource_id}</Td>
                                  <Td>{d.required_by?.join(', ') || '-'}</Td>
                                </Tr>
                              ))}
                            </Tbody>
                          </Table>
                        </div>
                      ))}
                    </ExpandableSection>
                  )}
                </CardBody>
              </CardExpandableContent>
            </Card>
          ))}
        </div>
      </Tab>

      <Tab eventKey={3} title={<TabTitleText>Quality</TabTitleText>}>
        <div style={{ padding: 16 }}>
          <Card style={{ marginBottom: 16 }}>
            <CardTitle>Quality Scores by Organization</CardTitle>
            <CardBody>
              <Table variant="compact">
                <Thead><Tr>
                  <Th>Organization</Th><Th>Score</Th><Th>Duplicates</Th><Th>Naming Style</Th><Th>Consistency</Th><Th>Violations</Th>
                </Tr></Thead>
                <Tbody>
                  {Object.entries(data.organizations).map(([name, org]) => {
                    const q = org.quality;
                    return (
                      <Tr key={name}>
                        <Td>{name}</Td>
                        <Td>{q ? <Label color={qualityColor(q.quality_score)} isCompact>{q.quality_score.toFixed(0)}</Label> : '-'}</Td>
                        <Td>{q?.duplicate_count ?? '-'}</Td>
                        <Td>{q?.naming_pattern?.dominant_pattern ?? '-'}</Td>
                        <Td>{q?.naming_pattern?.consistency_score != null ? `${q.naming_pattern.consistency_score.toFixed(0)}%` : '-'}</Td>
                        <Td>{q?.naming_pattern?.violations?.length ?? '-'}</Td>
                      </Tr>
                    );
                  })}
                </Tbody>
              </Table>
            </CardBody>
          </Card>

          {Object.entries(data.organizations)
            .filter(([, org]) => org.quality && org.quality.duplicate_count > 0)
            .map(([name, org]) => (
              <Card key={name} style={{ marginBottom: 8 }}>
                <CardTitle>{name} - Duplicates ({org.quality!.duplicate_count})</CardTitle>
                <CardBody>
                  {org.quality!.duplicates.map((dup, i) => (
                    <Card key={i} isFlat isCompact style={{ marginBottom: 8, padding: 12 }}>
                      <Split hasGutter>
                        <SplitItem>
                          <Label color={severityColor(dup.severity)} isCompact>{dup.severity.toUpperCase()}</Label>
                        </SplitItem>
                        <SplitItem isFilled>
                          <Text component="p"><strong>{dup.name}</strong> ({dup.resource_type}) - {dup.count} copies</Text>
                          <Text component="p" style={{ color: '#6a6e73', fontSize: '0.9em' }}>{dup.impact}</Text>
                          <Text component="p" style={{ fontSize: '0.9em' }}>Recommendation: {dup.recommendation}</Text>
                          {dup.ids.length > 0 && (
                            <Text component="p" style={{ fontSize: '0.85em', color: '#8a8d90' }}>IDs: {dup.ids.join(', ')}</Text>
                          )}
                        </SplitItem>
                      </Split>
                    </Card>
                  ))}
                </CardBody>
              </Card>
            ))}

          {Object.entries(data.organizations)
            .filter(([, org]) => org.quality?.naming_pattern?.case_style && Object.keys(org.quality.naming_pattern.case_style).length > 0)
            .length > 0 && (
            <Card style={{ marginTop: 16 }}>
              <CardTitle>Naming Convention Breakdown</CardTitle>
              <CardBody>
                <Table variant="compact">
                  <Thead><Tr><Th>Organization</Th><Th>Case Styles</Th><Th>Prefixes</Th><Th>Consistency</Th></Tr></Thead>
                  <Tbody>
                    {Object.entries(data.organizations)
                      .filter(([, org]) => org.quality?.naming_pattern)
                      .map(([name, org]) => {
                        const np = org.quality!.naming_pattern!;
                        return (
                          <Tr key={name}>
                            <Td>{name}</Td>
                            <Td>
                              {Object.entries(np.case_style || {}).map(([style, count]) => (
                                <Label key={style} isCompact style={{ margin: 1 }}>{style}: {count}</Label>
                              ))}
                            </Td>
                            <Td>
                              {Object.entries(np.prefixes || {}).map(([prefix, count]) => (
                                <Label key={prefix} isCompact style={{ margin: 1 }}>{prefix}: {count}</Label>
                              ))}
                            </Td>
                            <Td>
                              <Label color={qualityColor(np.consistency_score)} isCompact>
                                {np.consistency_score.toFixed(0)}%
                              </Label>
                            </Td>
                          </Tr>
                        );
                      })}
                  </Tbody>
                </Table>
              </CardBody>
            </Card>
          )}
        </div>
      </Tab>
    </Tabs>
  );
}
