import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@patternfly/react-core', () => ({
  Button: ({ children, onClick, isDisabled }: ButtonHTMLAttributes<HTMLButtonElement> & { isDisabled?: boolean }) => (
    <button disabled={isDisabled} onClick={onClick}>
      {children}
    </button>
  ),
  Card: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardBody: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardFooter: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardHeader: ({ children, actions }: { children: ReactNode; actions?: { actions: ReactNode } }) => (
    <div>
      {children}
      {actions?.actions}
    </div>
  ),
  CardTitle: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Title: ({ children }: { children: ReactNode }) => <h1>{children}</h1>,
  TextContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Text: ({ children }: { children: ReactNode }) => <p>{children}</p>,
  Gallery: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Label: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Split: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SplitItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DescriptionList: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DescriptionListGroup: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DescriptionListTerm: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  DescriptionListDescription: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Alert: ({ title }: { title: string }) => <div>{title}</div>,
  Divider: () => <hr />,
}));

vi.mock('@patternfly/react-core/deprecated', () => ({
  Dropdown: ({ toggle, dropdownItems }: { toggle: ReactNode; dropdownItems: ReactNode[] }) => (
    <div>
      {toggle}
      {dropdownItems}
    </div>
  ),
  DropdownItem: ({ children, onClick }: { children: ReactNode; onClick?: () => void }) => (
    <button onClick={onClick}>{children}</button>
  ),
  KebabToggle: ({ onToggle }: { onToggle: (_e: unknown, open: boolean) => void }) => (
    <button aria-label="menu" onClick={() => onToggle(undefined, true)}>
      menu
    </button>
  ),
}));

vi.mock('../components/ConnectionForm', () => ({
  ConnectionForm: ({
    isOpen,
    onSave,
    onClose,
    initial,
    error,
  }: {
    isOpen: boolean;
    onSave: (conn: Record<string, unknown>) => void;
    onClose: () => void;
    initial?: { id?: string };
    error?: string | null;
  }) =>
    isOpen ? (
      <div>
        <div>Connection Form {initial?.id ?? 'new'}</div>
        {error ? <div>{error}</div> : null}
        <button
          onClick={() =>
            onSave({
              name: 'Saved',
              type: 'awx',
              role: 'source',
              url: 'https://saved.example.com',
              token: 'token',
              verify_ssl: true,
            })
          }
        >
          Save Form
        </button>
        <button onClick={onClose}>Close Form</button>
      </div>
    ) : null,
}));

vi.mock('../api/client', () => ({
  api: {
    listConnections: vi.fn(),
    createConnection: vi.fn(),
    updateConnection: vi.fn(),
    deleteConnection: vi.fn(),
    testConnection: vi.fn(),
    clearMigrationState: vi.fn(),
  },
}));

import { api } from '../api/client';
import { Dashboard } from './Dashboard';

describe('Dashboard', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('loads connections and handles testing, deleting, editing, and clearing state', async () => {
    vi.mocked(api.listConnections).mockResolvedValue([
      {
        id: 'src-1',
        name: 'Source',
        type: 'awx',
        role: 'source',
        url: 'https://src.example.com',
        token: 'secret',
        verify_ssl: true,
        ping_status: 'ok',
        auth_status: 'ok',
      },
      {
        id: 'dst-1',
        name: 'Destination',
        type: 'aap',
        role: 'destination',
        url: 'https://dst.example.com',
        token: '',
        verify_ssl: false,
        ping_status: 'error',
        ping_error: 'offline',
        auth_status: 'error',
        auth_error: 'bad token',
      },
    ]);
    vi.mocked(api.testConnection).mockResolvedValue({ ok: true });
    vi.mocked(api.deleteConnection).mockResolvedValue(undefined);
    vi.mocked(api.updateConnection).mockResolvedValue({});
    vi.mocked(api.clearMigrationState).mockResolvedValue({
      cleared_progress: 2,
      deleted_mappings: 3,
    });

    render(<Dashboard />);

    expect(await screen.findByText('Source')).toBeInTheDocument();
    expect(screen.getByText('Destination')).toBeInTheDocument();
    expect(screen.getByText('Ping OK')).toBeInTheDocument();
    expect(screen.getByText('Unreachable')).toBeInTheDocument();
    expect(screen.getByText('bad token')).toBeInTheDocument();

    fireEvent.click(screen.getAllByText('Test')[0]);
    await waitFor(() => expect(api.testConnection).toHaveBeenCalledWith('src-1'));

    fireEvent.click(screen.getAllByLabelText('menu')[0]);
    fireEvent.click(screen.getAllByText('Edit')[0]);
    expect(screen.getByText('Connection Form src-1')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Save Form'));
    await waitFor(() => expect(api.updateConnection).toHaveBeenCalledWith('src-1', expect.any(Object)));

    fireEvent.click(screen.getAllByText('Delete')[0]);
    await waitFor(() => expect(api.deleteConnection).toHaveBeenCalledWith('src-1'));

    fireEvent.click(screen.getByText('Clear Migration State'));
    expect(
      await screen.findByText('Cleared 2 progress records and 3 ID mappings')
    ).toBeInTheDocument();
  });

  it('opens the add connection form and shows save errors', async () => {
    vi.mocked(api.listConnections).mockResolvedValue([]);
    vi.mocked(api.createConnection).mockRejectedValue(new Error('save failed'));

    render(<Dashboard />);

    expect(
      await screen.findByText("No connections yet. Click 'Add Connection' to get started.")
    ).toBeInTheDocument();

    fireEvent.click(screen.getByText('Add Connection'));
    expect(screen.getByText('Connection Form new')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Save Form'));
    expect(await screen.findByText('save failed')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Close Form'));
    await waitFor(() => expect(screen.queryByText('Connection Form new')).not.toBeInTheDocument());
  });
});
