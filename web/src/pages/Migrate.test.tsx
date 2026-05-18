import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
} from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const navigate = vi.fn();
const useJobLogsMock = vi.fn();

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
  Split: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SplitItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Flex: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  FlexItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
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
    'aria-label': ariaLabel,
  }: SelectHTMLAttributes<HTMLSelectElement> & {
    onChange: (_e: unknown, value: string) => void;
  }) => (
    <select
      id={id}
      aria-label={ariaLabel || id}
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
    id,
    value,
    onChange,
    placeholder,
  }: InputHTMLAttributes<HTMLInputElement> & {
    id?: string;
    value: string;
    onChange: (_e: unknown, value: string) => void;
  }) => (
    <input
      id={id}
      aria-label={id}
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event, event.currentTarget.value)}
    />
  ),
  Spinner: () => <div>Loading...</div>,
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
  FormHelperText: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  HelperText: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  HelperTextItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Tabs: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Tab: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  TabTitleText: ({ children }: { children: ReactNode }) => <span>{children}</span>,
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
  LogViewer: ({
    jobId,
    onClose,
  }: {
    jobId: string;
    onClose?: (status: string) => void;
  }) => (
    <div>
      LogViewer {jobId}
      {onClose ? <button onClick={() => onClose('cancelled')}>Close Logs</button> : null}
    </div>
  ),
}));

vi.mock('../components/MigrationProgressView', () => ({
  MigrationProgressView: ({ jobStatus }: { jobStatus: string }) => (
    <div>MigrationProgress {jobStatus}</div>
  ),
}));

vi.mock('../components/MigrationPreview', () => ({
  MigrationPreview: ({ preview }: { preview: { resources?: Record<string, unknown> } }) => (
    <div>Preview {Object.keys(preview.resources || {}).length}</div>
  ),
}));

vi.mock('../hooks/useJobLogs', () => ({
  useJobLogs: (...args: unknown[]) => useJobLogsMock(...args),
}));

vi.mock('../api/client', () => ({
  api: {
    listConnections: vi.fn(),
    listOrganizations: vi.fn(),
    migrationPreview: vi.fn(),
    getMigrationPreview: vi.fn(),
    migrationRun: vi.fn(),
    cancelJob: vi.fn(),
  },
}));

import { api } from '../api/client';
import { Migrate } from './Migrate';

describe('Migrate', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useJobLogsMock.mockReturnValue({
      status: 'running',
      events: [],
      textLines: ['line 1'],
    });
  });

  it('previews and runs a migration, then cancels and resets the wizard', async () => {
    vi.mocked(api.listConnections).mockResolvedValue([
      { id: 'src-1', name: 'Source', type: 'awx', url: 'https://src.example.com' },
      { id: 'dst-1', name: 'Destination', type: 'aap', url: 'https://dst.example.com' },
    ]);
    vi.mocked(api.listOrganizations).mockResolvedValue([
      { id: 1, name: 'Default', description: 'Primary' },
    ]);
    vi.mocked(api.migrationPreview).mockResolvedValue({ job_id: 'preview-1' });
    vi.mocked(api.getMigrationPreview).mockResolvedValue({
      status: 'completed',
      resources: { organizations: [{ id: 1 }] },
    });
    vi.mocked(api.migrationRun).mockResolvedValue({ job_id: 'run-1' });
    vi.mocked(api.cancelJob).mockResolvedValue({ status: 'cancelled' });

    render(<Migrate />);

    fireEvent.change(await screen.findByLabelText('Select source connection'), {
      target: { value: 'src-1' },
    });
    fireEvent.change(screen.getByLabelText('Select destination connection'), {
      target: { value: 'dst-1' },
    });
    expect(await screen.findByText(/Default/i)).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText(/Default — Primary/));
    fireEvent.change(screen.getByPlaceholderText('e.g. prod-east-'), {
      target: { value: 'prod-east-' },
    });
    fireEvent.click(screen.getByText('Preview Migration'));

    await waitFor(() =>
      expect(api.migrationPreview).toHaveBeenCalledWith('src-1', 'dst-1', [1])
    );

    await waitFor(
      () => expect(api.getMigrationPreview).toHaveBeenCalledWith('preview-1'),
      { timeout: 3000 }
    );
    expect(await screen.findByText('Preview 1', {}, { timeout: 3000 })).toBeInTheDocument();
    expect(screen.getByText('LogViewer preview-1')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Start Migration'));
    await waitFor(() =>
      expect(api.migrationRun).toHaveBeenCalledWith(
        'src-1',
        'dst-1',
        'preview-1',
        {},
        [1],
        'prod-east-'
      )
    );

    expect(await screen.findByText('LogViewer run-1')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Cancel Migration'));
    await waitFor(() => expect(api.cancelJob).toHaveBeenCalledWith('run-1'));

    fireEvent.click(screen.getByText('Open in Jobs'));
    expect(navigate).toHaveBeenCalledWith('/jobs/run-1');

    fireEvent.click(screen.getByText('Close Logs'));
    fireEvent.click(screen.getByText('New Migration'));

    expect(await screen.findByText('Preview Migration')).toBeInTheDocument();
  }, 10000);

  it('shows validation and launch errors during preview', async () => {
    vi.mocked(api.listConnections).mockResolvedValue([
      { id: 'same', name: 'Same', type: 'aap', url: 'https://same.example.com' },
      { id: 'dst-1', name: 'Destination', type: 'aap', url: 'https://dst.example.com' },
    ]);
    vi.mocked(api.listOrganizations).mockRejectedValue(new Error('org load failed'));

    render(<Migrate />);

    fireEvent.change(await screen.findByLabelText('Select source connection'), {
      target: { value: 'same' },
    });
    fireEvent.change(screen.getByLabelText('Select destination connection'), {
      target: { value: 'same' },
    });

    expect(
      await screen.findByText(/Source and destination cannot be the same connection/i)
    ).toBeInTheDocument();
    expect(screen.getByText('Preview Migration')).toBeDisabled();
    expect(screen.getByText('No organizations found on source')).toBeInTheDocument();
  });
});
