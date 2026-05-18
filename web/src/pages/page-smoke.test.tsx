import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ButtonHTMLAttributes, ReactNode, SelectHTMLAttributes } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const navigate = vi.fn();

vi.mock('@patternfly/react-core', () => ({
  Title: ({ children }: { children: ReactNode }) => <h1>{children}</h1>,
  Label: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Button: ({
    children,
    onClick,
    isDisabled,
  }: ButtonHTMLAttributes<HTMLButtonElement> & { isDisabled?: boolean }) => (
    <button disabled={isDisabled} onClick={onClick}>
      {children}
    </button>
  ),
  TextContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Text: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  Card: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardBody: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  FormGroup: ({ children, label }: { children: ReactNode; label?: string }) => (
    <label>
      {label}
      {children}
    </label>
  ),
  FormSelect: ({
    id,
    value,
    onChange,
    children,
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
  Alert: ({ title }: { title: string }) => <div>{title}</div>,
  Split: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SplitItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Form: ({ children }: { children: ReactNode }) => <form>{children}</form>,
  Spinner: () => <div>Loading...</div>,
}));

vi.mock('@patternfly/react-table', () => ({
  Table: ({ children }: { children: ReactNode }) => <table>{children}</table>,
  Thead: ({ children }: { children: ReactNode }) => <thead>{children}</thead>,
  Tbody: ({ children }: { children: ReactNode }) => <tbody>{children}</tbody>,
  Tr: ({ children, onRowClick }: { children: ReactNode; onRowClick?: () => void }) => (
    <tr onClick={onRowClick}>{children}</tr>
  ),
  Th: ({ children }: { children: ReactNode }) => <th>{children}</th>,
  Td: ({ children }: { children: ReactNode }) => <td>{children}</td>,
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => navigate,
    useSearchParams: () => [new URLSearchParams(''), vi.fn()],
  };
});

vi.mock('../components/ResourceTable', () => ({
  ResourceTable: ({ resources }: { resources: Record<string, unknown>[] }) => (
    <div>ResourceTable {resources.length}</div>
  ),
}));

vi.mock('../api/client', () => ({
  api: {
    listJobs: vi.fn(),
    listConnections: vi.fn(),
    runAnalysis: vi.fn(),
    listResourceTypes: vi.fn(),
    listResources: vi.fn(),
  },
}));

import { api } from '../api/client';
import { Analysis } from './Analysis';
import { Jobs } from './Jobs';
import { ObjectBrowser } from './ObjectBrowser';

describe('page smoke tests', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('renders jobs, refreshes, and navigates to details', async () => {
    vi.mocked(api.listJobs).mockResolvedValue([
      {
        id: 'job-1',
        seq_id: 7,
        name: 'Migration',
        type: 'migrate',
        status: 'completed',
        started_at: '2024-01-01T00:00:00.000Z',
        finished_at: '2024-01-01T00:00:10.000Z',
      },
    ]);

    render(<Jobs />);

    expect(await screen.findByText('Migration')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Refresh'));
    fireEvent.click(screen.getByText('View'));

    expect(api.listJobs).toHaveBeenCalled();
    expect(navigate).toHaveBeenCalledWith('/jobs/job-1');
  });

  it('shows empty jobs state when there are no jobs', async () => {
    vi.mocked(api.listJobs).mockResolvedValue([]);

    render(<Jobs />);

    expect(
      await screen.findByText(/No jobs yet. Start an analysis or migration/i)
    ).toBeInTheDocument();
  });

  it('filters source connections and starts an analysis job', async () => {
    vi.mocked(api.listConnections).mockResolvedValue([
      { id: 'src', name: 'Source', url: 'https://src', role: 'source' },
      { id: 'dst', name: 'Target', url: 'https://dst', role: 'target' },
    ]);
    vi.mocked(api.runAnalysis).mockResolvedValue({ job_id: 'job-9' });

    render(<Analysis />);

    fireEvent.change(await screen.findByLabelText('conn'), { target: { value: 'src' } });
    fireEvent.click(screen.getByText('Run Analysis'));

    await waitFor(() => expect(api.runAnalysis).toHaveBeenCalledWith('src'));
    expect(navigate).toHaveBeenCalledWith('/jobs/job-9');
  });

  it('shows analysis errors when launching fails', async () => {
    vi.mocked(api.listConnections).mockResolvedValue([
      { id: 'src', name: 'Source', url: 'https://src', role: 'source' },
    ]);
    vi.mocked(api.runAnalysis).mockRejectedValue(new Error('launch failed'));

    render(<Analysis />);

    fireEvent.change(await screen.findByLabelText('conn'), { target: { value: 'src' } });
    fireEvent.click(screen.getByText('Run Analysis'));

    expect(await screen.findByText('launch failed')).toBeInTheDocument();
  });

  it('loads object browser resource types and resources', async () => {
    vi.mocked(api.listConnections).mockResolvedValue([
      { id: 'conn-1', name: 'Source', type: 'awx' },
    ]);
    vi.mocked(api.listResourceTypes).mockResolvedValue([
      { name: 'organizations', label: 'Organizations', api_path: 'organizations/' },
    ]);
    vi.mocked(api.listResources).mockResolvedValue([{ id: 1, name: 'Default' }]);

    render(<ObjectBrowser />);

    fireEvent.change(await screen.findByLabelText('conn-select'), {
      target: { value: 'conn-1' },
    });

    expect(await screen.findByText('ResourceTable 1')).toBeInTheDocument();
  });

  it('shows object browser empty state when resource load fails', async () => {
    vi.mocked(api.listConnections).mockResolvedValue([
      { id: 'conn-1', name: 'Source', type: 'awx' },
    ]);
    vi.mocked(api.listResourceTypes).mockResolvedValue([
      { name: 'organizations', label: 'Organizations', api_path: 'organizations/' },
    ]);
    vi.mocked(api.listResources).mockRejectedValue(new Error('boom'));

    render(<ObjectBrowser />);

    fireEvent.change(await screen.findByLabelText('conn-select'), {
      target: { value: 'conn-1' },
    });

    expect(await screen.findByText('No resources found for this type.')).toBeInTheDocument();
  });
});
