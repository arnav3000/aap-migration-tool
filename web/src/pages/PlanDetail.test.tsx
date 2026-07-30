import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
} from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const navigate = vi.fn();

vi.mock('@patternfly/react-core', () => ({
  Title: ({ children }: { children: ReactNode }) => <h1>{children}</h1>,
  TextContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Text: ({ children }: { children: ReactNode }) => <p>{children}</p>,
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
  Alert: ({ title }: { title: string }) => <div>{title}</div>,
  Label: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Split: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SplitItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Flex: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  FlexItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  FormSelect: ({
    value,
    onChange,
    children,
    id,
  }: SelectHTMLAttributes<HTMLSelectElement> & {
    onChange: (_e: unknown, value: string) => void;
  }) => (
    <select
      id={id}
      aria-label={id}
      value={value as string}
      onChange={(event) => onChange(event, event.currentTarget.value)}
    >
      {children}
    </select>
  ),
  FormSelectOption: ({
    value,
    label,
    isDisabled,
  }: {
    value: string;
    label: string;
    isDisabled?: boolean;
  }) => (
    <option value={value} disabled={isDisabled}>
      {label}
    </option>
  ),
  TextInput: ({
    value,
    onChange,
    placeholder,
  }: InputHTMLAttributes<HTMLInputElement> & {
    value: string;
    onChange: (_e: unknown, value: string) => void;
  }) => (
    <input
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event, event.currentTarget.value)}
    />
  ),
  Spinner: () => <div>Loading...</div>,
  Tabs: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Tab: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  TabTitleText: ({ children }: { children: ReactNode }) => <span>{children}</span>,
}));

vi.mock('@patternfly/react-table', () => ({
  Table: ({ children }: { children: ReactNode }) => <table>{children}</table>,
  Thead: ({ children }: { children: ReactNode }) => <thead>{children}</thead>,
  Tbody: ({ children }: { children: ReactNode }) => <tbody>{children}</tbody>,
  Tr: ({ children }: { children: ReactNode }) => <tr>{children}</tr>,
  Th: ({ children }: { children: ReactNode }) => <th>{children}</th>,
  Td: ({ children }: { children: ReactNode }) => <td>{children}</td>,
}));

vi.mock('@patternfly/react-icons/dist/esm/icons/plus-circle-icon', () => ({ default: () => <span>plus</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/times-icon', () => ({ default: () => <span>x</span> }));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => navigate,
    useParams: () => ({ id: 'plan-1' }),
  };
});

vi.mock('../components/PhaseEditor', () => ({
  PhaseEditor: ({
    phases,
    onChange,
  }: {
    phases: { id: string; phase_number: number; name: string; status: string; orgs: unknown[] }[];
    onChange: (phases: unknown[]) => void;
  }) => (
    <div>
      PhaseEditor {phases.length}
      <button
        onClick={() =>
          onChange([
            ...phases,
            {
              id: 'phase-2',
              phase_number: 2,
              name: 'Phase Two',
              status: 'pending',
              update_mode: false,
              resource_types: [],
              orgs: [],
            },
          ])
        }
      >
        Change Phases
      </button>
    </div>
  ),
}));

vi.mock('../api/client', () => ({
  api: {
    getPlan: vi.fn(),
    listConnections: vi.fn(),
    listJobs: vi.fn(),
    getAnalysisResult: vi.fn(),
    updatePlanPhases: vi.fn(),
    populatePlan: vi.fn(),
    executePlanPhase: vi.fn(),
  },
}));

import { api } from '../api/client';
import { PlanDetail } from './PlanDetail';

describe('PlanDetail', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads a plan, saves changes, populates phases, and executes a phase', async () => {
    const plan = {
      id: 'plan-1',
      name: 'Migration Plan',
      description: 'Consolidation',
      status: 'draft',
      sources: [
        {
          id: 'source-1',
          connection_id: 'conn-source-1',
          name_prefix: null,
          analysis_job_id: 'analysis-1',
        },
      ],
      phases: [
        {
          id: 'phase-1',
          phase_number: 1,
          name: 'Phase One',
          status: 'pending',
          update_mode: false,
          resource_types: [],
          job_id: 'job-existing',
          orgs: [{ id: 'org-1', source_id: 'source-1', org_id: 1, org_name: 'Default' }],
        },
      ],
    };
    vi.mocked(api.getPlan).mockResolvedValue(plan);
    vi.mocked(api.listConnections).mockResolvedValue([
      { id: 'conn-source-1', name: 'Source One', url: 'https://src1.example.com', role: 'source' },
      { id: 'conn-source-2', name: 'Source Two', url: 'https://src2.example.com', role: 'source' },
      { id: 'dest-1', name: 'Destination', url: 'https://dst.example.com', role: 'destination' },
    ]);
    vi.mocked(api.listJobs).mockResolvedValue([
      {
        id: 'analysis-1',
        seq_id: 7,
        name: 'Source One analysis',
        status: 'completed',
        started_at: '2026-05-18T18:00:00Z',
      },
    ]);
    vi.mocked(api.getAnalysisResult).mockResolvedValue({
      organizations: { Default: { org_id: 1, can_migrate_standalone: true } },
    });
    vi.mocked(api.updatePlanPhases).mockResolvedValue({});
    vi.mocked(api.populatePlan).mockResolvedValue({
      ...plan,
      phases: [
        ...plan.phases,
        {
          id: 'phase-2',
          phase_number: 2,
          name: 'Generated Phase',
          status: 'pending',
          update_mode: false,
          resource_types: [],
          orgs: [],
        },
      ],
    });
    vi.mocked(api.executePlanPhase).mockResolvedValue({ job_id: 'job-77' });

    render(<PlanDetail />);

    expect(await screen.findByText('Migration Plan')).toBeInTheDocument();
    expect(await screen.findByText('PhaseEditor 1')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Back to Plans'));
    expect(navigate).toHaveBeenCalledWith('/planner');

    fireEvent.click(screen.getByText('Change Phases'));
    fireEvent.click(screen.getByText('Save Plan'));
    await waitFor(() => expect(api.updatePlanPhases).toHaveBeenCalled());
    expect(await screen.findByText('Plan saved successfully')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Generate Plan'));
    await waitFor(() => expect(api.populatePlan).toHaveBeenCalledWith('plan-1'));
    expect(
      await screen.findByText(/Plan generated from analysis results/i)
    ).toBeInTheDocument();

    fireEvent.click(screen.getByText('View Job'));
    expect(navigate).toHaveBeenCalledWith('/jobs/job-existing');

    fireEvent.click(screen.getAllByText('Execute')[0]);
    await waitFor(() =>
      expect(api.executePlanPhase).toHaveBeenCalledWith('plan-1', 'phase-1')
    );
    expect(navigate).toHaveBeenCalledWith('/jobs/job-77');
  });

  it('adds a source and assigns an analysis scan', async () => {
    vi.mocked(api.getPlan).mockResolvedValue({
      id: 'plan-1',
      name: 'Draft Plan',
      description: '',
      status: 'draft',
      sources: [],
      phases: [],
    });
    vi.mocked(api.listConnections).mockResolvedValue([
      { id: 'conn-source-2', name: 'Source Two', url: 'https://src2.example.com', role: 'source' },
    ]);
    vi.mocked(api.listJobs).mockResolvedValue([
      {
        id: 'analysis-2',
        seq_id: 8,
        name: 'Source Two analysis',
        status: 'completed',
        started_at: '2026-05-18T19:00:00Z',
      },
    ]);
    vi.mocked(api.getAnalysisResult).mockResolvedValue({});
    vi.mocked(api.updatePlanPhases).mockResolvedValue({});
    vi.mocked(api.populatePlan).mockResolvedValue({
      id: 'plan-1',
      name: 'Draft Plan',
      description: '',
      status: 'draft',
      sources: [
        {
          id: 'new-source',
          connection_id: 'conn-source-2',
          name_prefix: 'wave-',
          analysis_job_id: 'analysis-2',
        },
      ],
      phases: [],
    });

    render(<PlanDetail />);

    expect(await screen.findByText('Draft Plan')).toBeInTheDocument();

    const addSourceSelect = screen
      .getAllByRole('combobox')
      .find((element) => within(element).queryByText('-- Add source --'));
    expect(addSourceSelect).toBeDefined();

    fireEvent.change(addSourceSelect!, { target: { value: 'conn-source-2' } });
    fireEvent.change(screen.getByPlaceholderText('Name prefix (optional)'), {
      target: { value: 'wave-' },
    });
    fireEvent.click(screen.getByText('Add Source'));

    expect(await screen.findByText(/Source Two \(https:\/\/src2.example.com\)/)).toBeInTheDocument();

    const analysisSelect = screen
      .getAllByRole('combobox')
      .find((element) => within(element).queryByText('-- Select completed scan --'));
    expect(analysisSelect).toBeDefined();

    fireEvent.change(analysisSelect!, { target: { value: 'analysis-2' } });
    fireEvent.click(screen.getByText('Generate Plan'));

    await waitFor(() => expect(api.populatePlan).toHaveBeenCalledWith('plan-1'));
  });
});
