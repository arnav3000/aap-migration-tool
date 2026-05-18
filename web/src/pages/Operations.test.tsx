import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const navigate = vi.fn();

vi.mock('@patternfly/react-core', () => ({
  Button: ({
    children,
    onClick,
    isDisabled,
    ...props
  }: ButtonHTMLAttributes<HTMLButtonElement> & { isDisabled?: boolean }) => (
    <button type="button" disabled={isDisabled} onClick={onClick} {...props}>
      {children}
    </button>
  ),
  Checkbox: ({
    id,
    label,
    isChecked,
    onChange,
  }: {
    id: string;
    label: string;
    isChecked: boolean;
    onChange: (_e: unknown, checked: boolean) => void;
  }) => (
    <label htmlFor={id}>
      {label}
      <input
        id={id}
        type="checkbox"
        checked={isChecked}
        onChange={(event) => onChange(event, event.currentTarget.checked)}
      />
    </label>
  ),
  Title: ({ children }: { children: ReactNode }) => <h1>{children}</h1>,
  TextContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Text: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  Alert: ({ title, children }: { title: string; children?: ReactNode }) => (
    <div>
      {title}
      {children}
    </div>
  ),
  Label: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Modal: ({
    isOpen,
    children,
    actions,
    title,
  }: {
    isOpen: boolean;
    children: ReactNode;
    actions?: ReactNode[];
    title: string;
  }) =>
    isOpen ? (
      <div>
        <h2>{title}</h2>
        {children}
        {actions}
      </div>
    ) : null,
  ModalVariant: { small: 'small' },
  Split: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SplitItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Flex: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  FlexItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Card: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardBody: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardTitle: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

vi.mock('@patternfly/react-icons/dist/esm/icons/times-icon', () => ({ default: () => <span>x</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/external-link-alt-icon', () => ({ default: () => <span>ext</span> }));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

vi.mock('../components/LogViewer', () => ({
  LogViewer: ({ jobId }: { jobId: string }) => <div>LogViewer {jobId}</div>,
}));

vi.mock('../api/client', () => ({
  api: {
    listConnections: vi.fn(),
    runCleanup: vi.fn(),
    runExport: vi.fn(),
  },
}));

import { api } from '../api/client';
import { Operations } from './Operations';

describe('Operations', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('runs export and cleanup jobs, navigates, and dismisses active jobs', async () => {
    vi.mocked(api.listConnections).mockResolvedValue([
      {
        id: 'src-1',
        name: 'Source',
        url: 'https://src.example.com',
        type: 'awx',
        role: 'source',
        ping_status: 'ok',
        auth_status: 'ok',
      },
      {
        id: 'dst-1',
        name: 'Destination',
        url: 'https://dst.example.com',
        type: 'aap',
        role: 'destination',
        ping_status: 'error',
        ping_error: 'offline',
        auth_status: 'error',
        auth_error: 'bad token',
      },
    ]);
    vi.mocked(api.runExport).mockResolvedValue({ job_id: 'job-export' });
    vi.mocked(api.runCleanup).mockResolvedValue({ job_id: 'job-cleanup' });

    render(<Operations />);

    expect(await screen.findByText('Source')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Source'));
    fireEvent.click(screen.getByText('Destination'));
    expect(
      screen.getByText((content) => content.includes('authentication failed') && content.includes('bad token'))
    ).toBeInTheDocument();

    fireEvent.click(screen.getByText('Source'));
    fireEvent.click(screen.getByText('Browse'));
    expect(navigate).toHaveBeenCalledWith('/browse?conn=src-1');

    fireEvent.click(screen.getByText('Export'));
    await waitFor(() => expect(api.runExport).toHaveBeenCalledWith('src-1'));
    expect(await screen.findByText('LogViewer job-export')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Open in Jobs'));
    expect(navigate).toHaveBeenCalledWith('/jobs/job-export');

    fireEvent.click(screen.getByLabelText('Dismiss'));
    await waitFor(() =>
      expect(screen.queryByText('LogViewer job-export')).not.toBeInTheDocument()
    );

    fireEvent.click(screen.getByText('Cleanup'));
    expect(screen.getAllByText('Confirm Cleanup')[0]).toBeInTheDocument();
    fireEvent.click(
      screen.getByLabelText(/I understand this will permanently delete all resources/i)
    );
    fireEvent.click(screen.getAllByText('Confirm Cleanup')[1]);

    await waitFor(() => expect(api.runCleanup).toHaveBeenCalledWith('src-1'));
    expect(await screen.findByText('LogViewer job-cleanup')).toBeInTheDocument();
  });

  it('shows the empty state for operations', async () => {
    vi.mocked(api.listConnections).mockResolvedValue([]);

    render(<Operations />);

    expect(await screen.findByText(/No connections configured/i)).toBeInTheDocument();
  });

  it('shows operation errors', async () => {
    vi.mocked(api.listConnections).mockResolvedValueOnce([
      {
        id: 'src-1',
        name: 'Source',
        url: 'https://src.example.com',
        type: 'awx',
        role: 'source',
        ping_status: 'ok',
        auth_status: 'ok',
      },
    ]);
    vi.mocked(api.runExport).mockRejectedValue(new Error('export failed'));

    render(<Operations />);

    fireEvent.click(await screen.findByText('Source'));
    fireEvent.click(screen.getByText('Export'));

    expect(await screen.findByText('export failed')).toBeInTheDocument();
  });
});
