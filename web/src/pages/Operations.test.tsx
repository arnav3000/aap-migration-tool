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
  }: ButtonHTMLAttributes<HTMLButtonElement> & { isDisabled?: boolean; isLoading?: boolean }) => (
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
  Divider: () => <hr />,
  Title: ({ children }: { children: ReactNode }) => <h1>{children}</h1>,
  TextContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Text: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  TextInput: ({ value, onChange, ...props }: { value: string; onChange: (_e: unknown, v: string) => void; [key: string]: unknown }) => (
    <input type="text" value={value} onChange={(e) => onChange(e, (e.target as HTMLInputElement).value)} {...props} />
  ),
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
  FormGroup: ({ children, label }: { children: ReactNode; label?: string }) => (
    <div>
      {label ? <label>{label}</label> : null}
      {children}
    </div>
  ),
  FormHelperText: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  HelperText: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  HelperTextItem: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Card: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardBody: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardTitle: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  MenuToggle: ({ children, onClick }: { children: ReactNode; onClick: () => void }) => (
    <button type="button" onClick={onClick}>{children}</button>
  ),
  Select: ({
    children,
    isOpen,
    onSelect,
    onOpenChange,
    selected,
  }: {
    children: ReactNode;
    isOpen: boolean;
    onSelect: (_e: unknown, value: string) => void;
    onOpenChange: (open: boolean) => void;
    selected?: string;
  }) => (
    <div data-selected={selected}>
      <button type="button" onClick={() => onOpenChange(!isOpen)}>toggle-select</button>
      {isOpen ? (
        <div
          onClick={(event) => {
            const target = event.target as HTMLElement;
            const option = target.closest('[data-value]');
            if (option) {
              onSelect(event, option.getAttribute('data-value') as string);
              onOpenChange(false);
            }
          }}
        >
          {children}
        </div>
      ) : null}
    </div>
  ),
  SelectOption: ({ children, value }: { children: ReactNode; value: string }) => <div data-value={value}>{children}</div>,
  SelectList: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Spinner: () => <span>loading...</span>,
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
    runResourceScan: vi.fn(),
    listResources: vi.fn(),
    selectiveMigrate: vi.fn(),
  },
}));

import { api } from '../api/client';
import { Operations } from './Operations';

describe('Operations', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('runs resource scan and cleanup jobs, navigates, and dismisses active jobs', async () => {
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
    vi.mocked(api.runResourceScan).mockResolvedValue({ job_id: 'job-scan' });
    vi.mocked(api.runCleanup).mockResolvedValue({ job_id: 'job-cleanup' });

    render(<Operations />);

    const sourceButtons = await screen.findAllByText('Source');
    expect(sourceButtons.length).toBeGreaterThan(0);

    fireEvent.click(sourceButtons[0]);
    const destButtons = screen.getAllByText('Destination');
    fireEvent.click(destButtons[0]);
    expect(
      screen.getByText((content) => content.includes('authentication failed') && content.includes('bad token'))
    ).toBeInTheDocument();

    fireEvent.click(sourceButtons[0]);
    fireEvent.click(screen.getByText('Browse'));
    expect(navigate).toHaveBeenCalledWith('/browse?conn=src-1');

    fireEvent.click(screen.getByText('Scan Resources'));
    await waitFor(() => expect(api.runResourceScan).toHaveBeenCalledWith('src-1'));
    expect(await screen.findByText('LogViewer job-scan')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Open in Jobs'));
    expect(navigate).toHaveBeenCalledWith('/jobs/job-scan');

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
    vi.mocked(api.runResourceScan).mockRejectedValue(new Error('scan failed'));

    render(<Operations />);

    const srcButtons = await screen.findAllByText('Source');
    fireEvent.click(srcButtons[0]);
    fireEvent.click(screen.getByText('Scan Resources'));

    expect(await screen.findByText('scan failed')).toBeInTheDocument();
  });

  it('loads templates and runs selective migration', async () => {
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
        ping_status: 'ok',
        auth_status: 'ok',
      },
    ]);
    vi.mocked(api.listResources).mockImplementation(async (_connId, type) => {
      if (type === 'job_templates') {
        return [
          {
            id: 1,
            name: 'JT One',
            summary_fields: {
              organization: { name: 'Org' },
              project: { name: 'Proj' },
            },
          },
        ];
      }
      if (type === 'workflow_job_templates') {
        return [
          {
            id: 2,
            name: 'WF One',
            summary_fields: {
              organization: { name: 'Org' },
              inventory: { name: 'Inv' },
            },
          },
        ];
      }
      return [];
    });
    vi.mocked(api.selectiveMigrate).mockResolvedValue({ job_id: 'sel-job' });

    render(<Operations />);

    await screen.findByText('Selective Template Migration');

    const toggles = screen.getAllByText('toggle-select');
    fireEvent.click(toggles[0]);
    fireEvent.click(document.querySelector('[data-value="src-1"]') as HTMLElement);
    fireEvent.click(toggles[1]);
    fireEvent.click(document.querySelector('[data-value="dst-1"]') as HTMLElement);

    expect(await screen.findByText('JT One')).toBeInTheDocument();
    expect(screen.getByText('WF One')).toBeInTheDocument();

    fireEvent.click(screen.getByLabelText('Select JT One'));
    fireEvent.click(screen.getByLabelText('Select workflow WF One'));

    fireEvent.change(screen.getByPlaceholderText('e.g. dev_'), {
      target: { value: 'dev_' },
    });

    fireEvent.click(screen.getByText('Migrate 2 templates'));

    await waitFor(() =>
      expect(api.selectiveMigrate).toHaveBeenCalledWith('src-1', 'dst-1', [1], [2], false, 'dev_')
    );
    expect(await screen.findByText('LogViewer sel-job')).toBeInTheDocument();
  });

  it('shows selective migration errors', async () => {
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
        ping_status: 'ok',
        auth_status: 'ok',
      },
    ]);
    vi.mocked(api.listResources).mockRejectedValue(new Error('template load failed'));

    render(<Operations />);

    await screen.findByText('Selective Template Migration');
    const toggles = screen.getAllByText('toggle-select');
    fireEvent.click(toggles[0]);
    fireEvent.click(document.querySelector('[data-value="src-1"]') as HTMLElement);

    await waitFor(() =>
      expect(screen.getByText('template load failed')).toBeInTheDocument()
    );
  });
});
