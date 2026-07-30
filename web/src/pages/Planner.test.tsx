import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ButtonHTMLAttributes, ReactNode, SelectHTMLAttributes, TextareaHTMLAttributes } from 'react';
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
  Alert: ({ title }: { title: string }) => <div>{title}</div>,
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
  Form: ({ children }: { children: ReactNode }) => <form>{children}</form>,
  FormGroup: ({ children, label }: { children: ReactNode; label: string }) => (
    <label>
      {label}
      {children}
    </label>
  ),
  TextInput: ({
    id,
    value,
    onChange,
    placeholder,
  }: {
    id: string;
    value: string;
    onChange: (_e: unknown, value: string) => void;
    placeholder?: string;
  }) => (
    <input
      id={id}
      aria-label={id}
      value={value}
      placeholder={placeholder}
      onChange={(event) => onChange(event, event.currentTarget.value)}
    />
  ),
  TextArea: ({
    id,
    value,
    onChange,
    placeholder,
  }: TextareaHTMLAttributes<HTMLTextAreaElement> & {
    id: string;
    value: string;
    onChange: (_e: unknown, value: string) => void;
  }) => (
    <textarea
      id={id}
      aria-label={id}
      value={value}
      placeholder={placeholder}
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
  Label: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Split: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SplitItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

vi.mock('@patternfly/react-table', () => ({
  Table: ({ children }: { children: ReactNode }) => <table>{children}</table>,
  Thead: ({ children }: { children: ReactNode }) => <thead>{children}</thead>,
  Tbody: ({ children }: { children: ReactNode }) => <tbody>{children}</tbody>,
  Tr: ({ children }: { children: ReactNode }) => <tr>{children}</tr>,
  Th: ({ children }: { children: ReactNode }) => <th>{children}</th>,
  Td: ({ children }: { children: ReactNode }) => <td>{children}</td>,
}));

vi.mock('react-router-dom', async () => {
  const actual = await vi.importActual<typeof import('react-router-dom')>('react-router-dom');
  return {
    ...actual,
    useNavigate: () => navigate,
  };
});

vi.mock('../api/client', () => ({
  api: {
    listPlans: vi.fn(),
    listConnections: vi.fn(),
    createPlan: vi.fn(),
    deletePlan: vi.fn(),
  },
}));

import { api } from '../api/client';
import { Planner } from './Planner';

describe('Planner', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('shows the empty state and surfaces create errors', async () => {
    vi.mocked(api.listPlans).mockResolvedValue([]);
    vi.mocked(api.listConnections).mockResolvedValue([
      { id: 'dest-1', name: 'Destination', url: 'https://dest.example.com', type: 'aap', role: 'destination' },
    ]);
    vi.mocked(api.createPlan).mockRejectedValue(new Error('create failed'));

    render(<Planner />);

    expect(await screen.findByText('No migration plans')).toBeInTheDocument();

    fireEvent.click(screen.getAllByText('Create Plan')[0]);
    fireEvent.change(screen.getByLabelText('plan-name'), {
      target: { value: 'Prod Migration' },
    });
    fireEvent.change(screen.getByLabelText('plan-desc'), {
      target: { value: 'Main move' },
    });
    fireEvent.change(screen.getByLabelText('plan-dest'), {
      target: { value: 'dest-1' },
    });
    fireEvent.click(screen.getByText('Create'));

    expect(await screen.findByText('create failed')).toBeInTheDocument();

    fireEvent.click(screen.getByText('Cancel'));
    await waitFor(() =>
      expect(screen.queryByText('Create Migration Plan')).not.toBeInTheDocument()
    );
  });

  it('lists plans, navigates to details, deletes plans, and creates successfully', async () => {
    vi.mocked(api.listPlans)
      .mockResolvedValueOnce([
        {
          id: 'plan-1',
          name: 'Wave One',
          description: 'First wave',
          status: 'draft',
          source_count: 2,
          phase_count: 3,
          updated_at: '2026-05-18T18:00:00Z',
        },
      ])
      .mockResolvedValueOnce([]);
    vi.mocked(api.listConnections).mockResolvedValue([
      { id: 'dest-1', name: 'Destination', url: 'https://dest.example.com', type: 'aap', role: 'destination' },
    ]);
    vi.mocked(api.deletePlan).mockResolvedValue(undefined);
    vi.mocked(api.createPlan).mockResolvedValue({ id: 'plan-2' });

    render(<Planner />);

    expect(await screen.findByText('Wave One')).toBeInTheDocument();
    fireEvent.click(screen.getByText('Wave One'));
    expect(navigate).toHaveBeenCalledWith('/planner/plan-1');

    fireEvent.click(screen.getByText('Delete'));
    await waitFor(() => expect(api.deletePlan).toHaveBeenCalledWith('plan-1'));
    await waitFor(() => expect(api.listPlans).toHaveBeenCalledTimes(2));

    fireEvent.click(screen.getAllByText('Create Plan')[0]);
    fireEvent.change(screen.getByLabelText('plan-name'), {
      target: { value: 'Wave Two' },
    });
    fireEvent.change(screen.getByLabelText('plan-dest'), {
      target: { value: 'dest-1' },
    });
    fireEvent.click(screen.getByText('Create'));

    await waitFor(() =>
      expect(api.createPlan).toHaveBeenCalledWith({
        name: 'Wave Two',
        description: '',
        destination_id: 'dest-1',
      })
    );
    expect(navigate).toHaveBeenCalledWith('/planner/plan-2');
  });
});
