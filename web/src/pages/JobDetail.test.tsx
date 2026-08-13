import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const navigate = vi.fn();
const useJobLogsMock = vi.fn();

const apiMockState = vi.hoisted(() => ({
  analysisResult: { status: 'completed', data: null as Record<string, unknown> | null },
}));

vi.mock('@patternfly/react-core', () => ({
  Button: ({
    children,
    onClick,
    isDisabled,
    isLoading,
    ...props
  }: ButtonHTMLAttributes<HTMLButtonElement> & { isDisabled?: boolean; isLoading?: boolean }) => (
    <button type="button" disabled={isDisabled || isLoading} onClick={onClick} {...props}>
      {children}
    </button>
  ),
  Title: ({ children }: { children: ReactNode }) => <h1>{children}</h1>,
  Label: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Split: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SplitItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Card: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardBody: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DescriptionList: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DescriptionListGroup: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DescriptionListTerm: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DescriptionListDescription: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Spinner: () => <div>Loading spinner</div>,
  Alert: ({ title, children }: { title: string; children?: ReactNode }) => (
    <div>
      {title}
      {children}
    </div>
  ),
  Tabs: ({
    children,
    onSelect,
  }: {
    children: ReactNode;
    onSelect?: (_e: unknown, key: string) => void;
  }) => (
    <div>
      {children}
      <button type="button" onClick={() => onSelect?.(undefined, 'credentials')}>Credentials Tab</button>
      <button type="button" onClick={() => onSelect?.(undefined, 'logs')}>Logs Tab</button>
    </div>
  ),
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

vi.mock('@patternfly/react-icons/dist/esm/icons/arrow-left-icon', () => ({
  default: () => <span>back</span>,
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => navigate,
    useParams: () => ({ id: 'job-1' }),
  };
});

vi.mock('../components/LogViewer', () => ({
  LogViewer: ({
    jobId,
    onClose,
  }: {
    jobId: string;
    onClose?: (status: string) => void;
  }) => (
    <div>
      LogViewer {jobId}
      {onClose ? <button type="button" onClick={() => onClose('completed')}>Close Logs</button> : null}
    </div>
  ),
}));

vi.mock('../components/MigrationProgressView', () => ({
  MigrationProgressView: ({ jobStatus }: { jobStatus: string }) => (
    <div>MigrationProgress {jobStatus}</div>
  ),
}));

vi.mock('../components/AnalysisResults', () => ({
  AnalysisResults: () => <div>Analysis Results View</div>,
}));

vi.mock('../hooks/useJobLogs', () => ({
  useJobLogs: (...args: unknown[]) => useJobLogsMock(...args),
}));

vi.mock('../api/client', () => ({
  api: {
    getJob: vi.fn(),
    cancelJob: vi.fn(),
    resumeJob: vi.fn(),
    getAnalysisResult: vi.fn(async () => apiMockState.analysisResult),
    exportAnalysisJson: vi.fn((id: string) => `/api/jobs/${id}/analysis.json`),
    exportAnalysisHtml: vi.fn((id: string) => `/api/jobs/${id}/analysis.html`),
    getJobCredentialsCsvUrl: vi.fn((id: string) => `/api/jobs/${id}/credentials.csv`),
  },
}));

import { api } from '../api/client';
import { JobDetail } from './JobDetail';

const analysisData = {
  analysis_date: '2026-01-01',
  source_url: 'https://src.example.com',
  total_organizations: 1,
  analyzed_organizations: ['Default'],
  independent_orgs: ['Default'],
  dependent_orgs: [],
  migration_order: ['Default'],
  migration_phases: [{ phase: 1, orgs: ['Default'], description: 'Phase 1' }],
  organizations: {
    Default: {
      org_id: 1,
      resource_count: 5,
      has_cross_org_deps: false,
      can_migrate_standalone: true,
      required_migrations_before: [],
      blocks: [],
      dependencies: {},
      quality: null,
      resources: { inventory: 5 },
    },
  },
  global_resources: {},
  total_duplicates: 0,
  average_quality_score: 80,
  circular_dependencies: [],
};

describe('JobDetail', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useJobLogsMock.mockReturnValue({
      status: 'completed',
      events: [],
      textLines: [],
      migration: null,
    });
  });

  it('shows loading spinner while job loads', () => {
    vi.mocked(api.getJob).mockImplementation(() => new Promise(() => undefined));
    render(<JobDetail />);
    expect(screen.getByText('Loading spinner')).toBeInTheDocument();
  });

  it('shows error when job load fails', async () => {
    vi.mocked(api.getJob).mockRejectedValue(new Error('Network error'));
    render(<JobDetail />);
    expect(await screen.findByText('Network error')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Back to Jobs'));
    expect(navigate).toHaveBeenCalledWith('/jobs');
  });

  it('renders completed analysis job with results and export links', async () => {
    apiMockState.analysisResult = { status: 'completed', data: analysisData };
    vi.mocked(api.getJob).mockResolvedValue({
      id: 'job-1',
      seq_id: 7,
      type: 'analysis',
      status: 'completed',
      started_at: '2026-01-01T10:00:00Z',
      finished_at: '2026-01-01T10:05:00Z',
    });

    render(<JobDetail />);

    expect(await screen.findByText(/Job #7: analysis/)).toBeInTheDocument();
    expect(screen.getByText('Download JSON')).toHaveAttribute('href', '/api/jobs/job-1/analysis.json');
    expect(screen.getByText('Download HTML Report')).toHaveAttribute('href', '/api/jobs/job-1/analysis.html');
    await waitFor(() => expect(api.getAnalysisResult).toHaveBeenCalledWith('job-1'));
    expect(await screen.findByText('Results')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Logs Tab'));
    expect(screen.getByText('LogViewer job-1')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Close Logs'));
  });

  it('cancels a running job', async () => {
    vi.mocked(api.getJob).mockResolvedValue({
      id: 'job-1',
      seq_id: 8,
      type: 'analysis',
      status: 'running',
      started_at: '2026-01-01T10:00:00Z',
    });
    vi.mocked(api.cancelJob).mockResolvedValue({ status: 'cancelled' });

    render(<JobDetail />);
    expect(await screen.findByText('Cancel Job')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Cancel Job'));
    await waitFor(() => expect(api.cancelJob).toHaveBeenCalledWith('job-1'));
  });

  it('renders migration run with credentials and resumes to new job', async () => {
    useJobLogsMock.mockReturnValue({
      status: 'waiting_for_input',
      events: [
        {
          _event: 'credential_pause',
          credentials: [
            {
              name: 'vault-cred',
              credential_type: 'vault',
              organization: 'Default',
              source: 'source-aap',
              name_prefix: 'copy-',
              used_by: [{ resource_type: 'job_template', resource_name: 'Deploy' }],
            },
          ],
        },
      ],
      textLines: ['log line'],
      migration: { phases: [] },
    });

    vi.mocked(api.getJob).mockResolvedValue({
      id: 'job-1',
      seq_id: 9,
      type: 'migration-run',
      status: 'waiting_for_input',
      started_at: '2026-01-01T10:00:00Z',
      result: {
        credential_review: [
          {
            name: 'vault-cred',
            credential_type: 'vault',
            organization: 'Default',
            used_by: [{ resource_type: 'job_template', resource_name: 'Deploy' }],
          },
        ],
      },
    });
    vi.mocked(api.resumeJob).mockResolvedValue({ status: 'running', new_job_id: 'job-2' });

    render(<JobDetail />);

    expect(await screen.findByText(/Job #9: migration-run/)).toBeInTheDocument();
    expect(screen.getByText('Migration paused — update credential secrets on the target before continuing.')).toBeInTheDocument();
    expect(screen.getByText('MigrationProgress waiting_for_input')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Credentials Tab'));
    expect(screen.getByText('vault-cred')).toBeInTheDocument();
    expect(screen.getByText('Download CSV')).toHaveAttribute('href', '/api/jobs/job-1/credentials.csv');

    fireEvent.click(screen.getAllByText('Continue Migration')[0]);
    await waitFor(() => expect(api.resumeJob).toHaveBeenCalledWith('job-1'));
    expect(navigate).toHaveBeenCalledWith('/jobs/job-2');
  });

  it('shows plain log viewer for non-analysis running jobs without results', async () => {
    vi.mocked(api.getJob).mockResolvedValue({
      id: 'job-1',
      seq_id: 10,
      type: 'migration-preview',
      status: 'running',
      started_at: '2026-01-01T10:00:00Z',
      error: 'partial failure',
    });

    render(<JobDetail />);

    expect(await screen.findByText(/Job #10: migration-preview/)).toBeInTheDocument();
    expect(screen.getByText('partial failure')).toBeInTheDocument();
    expect(screen.getByText('LogViewer job-1')).toBeInTheDocument();
  });
});
