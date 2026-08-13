import { fireEvent, render, screen } from '@testing-library/react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

import { AnalysisResults, type AnalysisData } from './AnalysisResults';

vi.mock('@patternfly/react-core', () => {
  let activeTabKey = 0;
  return {
    Title: ({ children }: { children: ReactNode }) => <h3>{children}</h3>,
    Text: ({ children }: { children: ReactNode }) => <p>{children}</p>,
    Button: ({
      children,
      onClick,
    }: ButtonHTMLAttributes<HTMLButtonElement>) => (
      <button type="button" onClick={onClick}>{children}</button>
    ),
    Card: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    CardBody: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    CardTitle: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    Alert: ({ title, children }: { title: string; children?: ReactNode }) => (
      <div>
        {title}
        {children}
      </div>
    ),
    Label: ({ children }: { children: ReactNode }) => <span>{children}</span>,
    Split: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    SplitItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    Tabs: ({
      children,
      onSelect,
      activeKey,
    }: {
      children: ReactNode;
      onSelect?: (_e: unknown, key: number) => void;
      activeKey?: number;
    }) => {
      activeTabKey = activeKey ?? 0;
      return (
        <div data-active-tab={activeTabKey}>
          {children}
          <button type="button" onClick={() => onSelect?.(undefined, 1)}>Tab Phases</button>
          <button type="button" onClick={() => onSelect?.(undefined, 2)}>Tab Orgs</button>
          <button type="button" onClick={() => onSelect?.(undefined, 3)}>Tab Quality</button>
        </div>
      );
    },
    Tab: ({ children, eventKey }: { children: ReactNode; eventKey: number }) =>
      activeTabKey === eventKey ? <div>{children}</div> : null,
    TabTitleText: ({ children }: { children: ReactNode }) => <span>{children}</span>,
    ExpandableSection: ({ children, toggleText }: { children: ReactNode; toggleText: string }) => (
      <div>
        <span>{toggleText}</span>
        {children}
      </div>
    ),
    DescriptionList: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    DescriptionListGroup: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    DescriptionListTerm: ({ children }: { children: ReactNode }) => <div>{children}</div>,
    DescriptionListDescription: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  };
});

vi.mock('@patternfly/react-table', () => ({
  Table: ({ children }: { children: ReactNode }) => <table>{children}</table>,
  Thead: ({ children }: { children: ReactNode }) => <thead>{children}</thead>,
  Tbody: ({ children }: { children: ReactNode }) => <tbody>{children}</tbody>,
  Tr: ({ children }: { children: ReactNode }) => <tr>{children}</tr>,
  Th: ({ children }: { children: ReactNode }) => <th>{children}</th>,
  Td: ({ children }: { children: ReactNode }) => <td>{children}</td>,
}));

function makeAnalysisData(): AnalysisData {
  return {
    analysis_date: '2026-01-01',
    source_url: 'https://src.example.com',
    total_organizations: 3,
    analyzed_organizations: ['OrgA', 'OrgB', 'OrgC'],
    independent_orgs: ['OrgA'],
    dependent_orgs: ['OrgB', 'OrgC'],
    migration_order: ['OrgA', 'OrgB', 'OrgC'],
    migration_phases: [
      { phase: 1, orgs: ['OrgA', 'OrgB'], description: 'Phase 1' },
      { phase: 2, orgs: ['OrgC'], description: 'Phase 2' },
    ],
    organizations: {
      OrgA: {
        org_id: 1,
        resource_count: 10,
        has_cross_org_deps: false,
        can_migrate_standalone: true,
        required_migrations_before: [],
        blocks: ['OrgB'],
        dependencies: {},
        quality: {
          quality_score: 85,
          duplicate_count: 2,
          duplicates: [
            {
              name: 'dup-cred',
              resource_type: 'credential',
              count: 2,
              ids: [1, 2],
              severity: 'error',
              impact: 'Duplicate credentials',
              recommendation: 'Merge duplicates',
            },
            {
              name: 'dup-inv',
              resource_type: 'inventory',
              count: 3,
              ids: [3],
              severity: 'warning',
              impact: 'Duplicate inventories',
              recommendation: 'Consolidate',
            },
          ],
          naming_pattern: {
            dominant_pattern: 'snake_case',
            consistency_score: 90,
            total_resources: 10,
            case_style: { snake: 8, camel: 2 },
            prefixes: { app_: 5 },
            separators: { _: 10 },
            violations: ['bad name'],
          },
        },
        resources: { inventory: 5, credential: 3 },
      },
      OrgB: {
        org_id: 2,
        resource_count: 5,
        has_cross_org_deps: true,
        can_migrate_standalone: false,
        required_migrations_before: ['OrgA'],
        blocks: [],
        dependencies: {
          OrgA: [
            {
              resource_type: 'credential',
              resource_id: 1,
              resource_name: 'cred-1',
              required_by: ['job-template-1'],
            },
          ],
        },
        quality: {
          quality_score: 45,
          duplicate_count: 0,
          duplicates: [],
          naming_pattern: {
            dominant_pattern: 'mixed',
            consistency_score: 40,
            total_resources: 5,
            case_style: { mixed: 5 },
            prefixes: { dev_: 2 },
            separators: { '-': 3 },
            violations: [],
          },
        },
        resources: { project: 2 },
      },
      OrgC: {
        org_id: 3,
        resource_count: 3,
        has_cross_org_deps: true,
        can_migrate_standalone: false,
        required_migrations_before: ['OrgB'],
        blocks: [],
        dependencies: {},
        quality: null,
        resources: {},
      },
    },
    global_resources: { execution_environment: 2 },
    total_duplicates: 2,
    average_quality_score: 65,
    circular_dependencies: [['OrgB', 'OrgC']],
  };
}

describe('AnalysisResults', () => {
  it('shows warning when data is missing', () => {
    render(<AnalysisResults data={null as unknown as AnalysisData} />);
    expect(screen.getByText('No analysis data available')).toBeInTheDocument();
  });

  it('renders summary, phases, organizations, and quality tabs', () => {
    render(<AnalysisResults data={makeAnalysisData()} />);

    expect(screen.getByText('Total Orgs')).toBeInTheDocument();
    expect(screen.getByText('Circular Dependencies Detected')).toBeInTheDocument();
    expect(screen.getByText('Migration Blockers (Critical Path)')).toBeInTheDocument();
    expect(screen.getByText('Global Resources (not org-scoped)')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Tab Phases'));
    expect(screen.getByText('Phase 1 — 2 organizations')).toBeInTheDocument();
    expect(
      screen.getByText('Circular dependencies detected — all organizations placed in a single phase')
    ).toBeInTheDocument();

    fireEvent.click(screen.getByText('Tab Orgs'));
    expect(screen.getByText('OrgA')).toBeInTheDocument();
    fireEvent.click(screen.getAllByText('▶')[0]);
    expect(screen.getByText('Resource Breakdown')).toBeInTheDocument();
    expect(screen.getByText('Dependencies (1 org(s))')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Tab Quality'));
    expect(screen.getByText('Quality Scores by Organization')).toBeInTheDocument();
    expect(screen.getByText('OrgA - Duplicates (2)')).toBeInTheDocument();
    expect(screen.getByText('Naming Convention Breakdown')).toBeInTheDocument();
  });
});
