import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
} from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@patternfly/react-core', () => ({
  Title: ({ children }: { children: ReactNode }) => <h1>{children}</h1>,
  TextContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Text: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  Form: ({ children }: { children: ReactNode }) => <form>{children}</form>,
  FormGroup: ({ children, label }: { children: ReactNode; label: string }) => (
    <label>
      {label}
      {children}
    </label>
  ),
  FormSection: ({ children }: { children: ReactNode }) => <section>{children}</section>,
  TextInput: ({
    id,
    value,
    onChange,
  }: InputHTMLAttributes<HTMLInputElement> & {
    id: string;
    value: string;
    onChange: (_e: unknown, value: string) => void;
  }) => (
    <input
      id={id}
      aria-label={id}
      value={value}
      onChange={(event) => onChange(event, event.currentTarget.value)}
    />
  ),
  FormSelect: ({
    id,
    value,
    onChange,
    children,
  }: SelectHTMLAttributes<HTMLSelectElement> & {
    id: string;
    value: string;
    onChange: (_e: unknown, value: string) => void;
  }) => (
    <select
      id={id}
      aria-label={id}
      value={value}
      onChange={(event) => onChange(event, event.currentTarget.value)}
    >
      {children}
    </select>
  ),
  FormSelectOption: ({
    value,
    label,
  }: {
    value: string;
    label: string;
  }) => <option value={value}>{label}</option>,
  Button: ({
    children,
    onClick,
    isDisabled,
  }: ButtonHTMLAttributes<HTMLButtonElement> & { isDisabled?: boolean }) => (
    <button type="button" disabled={isDisabled} onClick={onClick}>
      {children}
    </button>
  ),
  Card: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardBody: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardTitle: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Gallery: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DescriptionList: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DescriptionListGroup: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DescriptionListTerm: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DescriptionListDescription: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Alert: ({ title, children }: { title: string; children?: ReactNode }) => (
    <div>
      {title}
      {children}
    </div>
  ),
  ExpandableSection: ({ children, toggleText }: { children: ReactNode; toggleText: string }) => (
    <div>
      <div>{toggleText}</div>
      {children}
    </div>
  ),
  Tabs: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Tab: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  TabTitleText: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Spinner: () => <div>Loading...</div>,
  Label: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Split: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SplitItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  List: ({ children }: { children: ReactNode }) => <ul>{children}</ul>,
  ListItem: ({ children }: { children: ReactNode }) => <li>{children}</li>,
}));

vi.mock('../api/client', () => ({
  api: {
    listConnections: vi.fn(),
    calculateSizing: vi.fn(),
    calculateDynamicSizing: vi.fn(),
  },
}));

import { api } from '../api/client';
import { Sizing } from './Sizing';

describe('Sizing', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('calculates manual and dynamic sizing results across both deployment targets', async () => {
    vi.mocked(api.listConnections).mockResolvedValue([
      {
        id: 'conn-1',
        name: 'Source',
        url: 'https://src.example.com',
        role: 'source',
        type: 'aap',
        ping_status: 'ok',
        auth_status: 'ok',
      },
    ]);
    vi.mocked(api.calculateSizing).mockResolvedValue({
      warnings: ['Scale carefully'],
      validation_warnings: ['Inputs normalized'],
      deployment: {
        target: 'containerized',
        recommended_topology: 'growth',
        growth_viable: true,
        doc_link: 'https://example.com/docs',
        growth_limitations: ['Redis on shared VM'],
        vm_count: 3,
        vm_spec: { cpu: 8, memory_gb: 32, disk_gb: 200 },
        vm_layout: [{ purpose: 'controller', count: 2 }],
        layout: '2 controller + 1 database',
        redis: 'shared',
        hub_storage: '50Gi',
        db_type: 'postgresql',
      },
      execution_nodes: { execution_pods: 4, cpu_per_pod: 2, memory_per_pod_gb: 8 },
      controller: { control_plane_pods: 2 },
      database: { storage_gb: 200 },
      automation_hub: { hub_pods: 1 },
      gateway: { gateway_pods: 1 },
      eda: { eda_pods: 0 },
      redis: { total_nodes: 1 },
    });
    vi.mocked(api.calculateDynamicSizing).mockResolvedValue({
      mode: 'dynamic',
      deployment_target: 'ocp',
      source_observed: {
        jobs_analyzed: 120,
        analysis_days: 30,
        version: '2.4',
        managed_hosts: 5000,
        total_instances: 6,
        instance_groups: 2,
        playbooks_per_day_peak: 300,
        playbooks_per_day_avg: 200,
        job_duration_hours_avg: 1.25,
        detected_peak_pattern: 'business_hours',
        total_current_cpu: 24,
        total_current_memory_gb: 96,
        avg_forks_configured: 25,
      },
      derived_inputs: { managed_hosts: 6250, playbooks_per_day_peak: 375 },
      headroom_multiplier: 1.25,
      recommendation: {
        deployment: {
          target: 'ocp',
          recommended_topology: 'enterprise',
          growth_viable: true,
          doc_link: 'https://example.com/ocp',
          enterprise_reasons: ['HA requirement'],
          cluster_type: 'standard',
          worker_nodes: 3,
          worker_spec: { cpu: 16, memory_gb: 64, disk_gb: 500 },
          total_nodes: 4,
          external_db: { cpu: 8, memory_gb: 32 },
          db_type: 'external postgres',
        },
        components: {
          automation_controller_execution_plane: { execution_pods: 6 },
          database: { cpu: 8 },
        },
        summary: { total_cpu: 64, total_memory_gb: 256 },
        warnings: ['Observed burst traffic'],
        deployment_notes: ['Use dedicated infra nodes'],
      },
    });

    render(<Sizing />);

    fireEvent.change(document.getElementById('manual-target')!, {
      target: { value: 'containerized' },
    });
    fireEvent.click(screen.getByText('Calculate'));

    await waitFor(() => expect(api.calculateSizing).toHaveBeenCalled());
    expect(await screen.findByText('Sizing Warnings')).toBeInTheDocument();
    expect(screen.getByText('Validation Notes')).toBeInTheDocument();
    expect(screen.getByText('CPU Per Instance')).toBeInTheDocument();
    expect(screen.getByText('Execution Instances')).toBeInTheDocument();
    expect(screen.getByText('Growth topology limitations')).toBeInTheDocument();
    expect(screen.getByText('Red Hat Tested Topology Documentation')).toBeInTheDocument();

    fireEvent.change(document.getElementById('dyn-target')!, {
      target: { value: 'ocp' },
    });
    fireEvent.click(screen.getByText('Analyze & Calculate'));

    await waitFor(() =>
      expect(api.calculateDynamicSizing).toHaveBeenCalledWith('conn-1', 30, 'ocp')
    );
    expect(await screen.findByText('Observed from Source AAP')).toBeInTheDocument();
    expect(screen.getByText('Why enterprise topology')).toBeInTheDocument();
    expect(screen.getByText('Deployment Notes')).toBeInTheDocument();
    expect(screen.getByText('Total Resource Summary')).toBeInTheDocument();
  });

  it('shows manual and dynamic errors when calculations fail', async () => {
    vi.mocked(api.listConnections).mockResolvedValue([]);
    vi.mocked(api.calculateSizing).mockRejectedValue(new Error('manual failed'));

    render(<Sizing />);

    fireEvent.click(screen.getByText('Calculate'));
    expect(await screen.findByText('manual failed')).toBeInTheDocument();
    expect(
      screen.getByText((content) => content.includes('No connections configured'))
    ).toBeInTheDocument();
  });
});
