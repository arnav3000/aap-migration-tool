import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import { api } from '../api/client';

vi.mock('@patternfly/react-core', () => ({
  Alert: ({ title, children }: { title: string; children?: ReactNode }) => (
    <div>
      <strong>{title}</strong>
      {children}
    </div>
  ),
  Checkbox: ({
    id,
    label,
    isChecked,
    onChange,
    ...props
  }: {
    id: string;
    label?: string;
    isChecked?: boolean;
    onChange: () => void;
  }) => (
    <label htmlFor={id}>
      {label}
      <input id={id} type="checkbox" checked={isChecked} onChange={onChange} {...props} />
    </label>
  ),
  Label: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  ExpandableSection: ({
    toggleText,
    isExpanded,
    onToggle,
    children,
  }: {
    toggleText: string;
    isExpanded: boolean;
    onToggle: () => void;
    children: ReactNode;
  }) => (
    <div>
      <button onClick={onToggle}>{toggleText}</button>
      {isExpanded ? children : null}
    </div>
  ),
}));

vi.mock('../api/client', () => ({
  api: {
    getExclusions: vi.fn(),
  },
}));

import { MigrationPreview } from './MigrationPreview';

describe('MigrationPreview', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.mocked(api.getExclusions).mockResolvedValue({
      migration: {},
      cleanup: {},
    });
  });

  it('renders preview counts, warnings, and default exclusions', async () => {
    vi.mocked(api.getExclusions).mockResolvedValue({
      migration: { organizations: ['Default'] },
      cleanup: {},
    });

    render(
      <MigrationPreview
        preview={{
          source_id: 'src',
          destination_id: 'dst',
          warnings: ['Check credentials'],
          host_counts: { InventoryA: 3 },
          group_counts: { InventoryA: 1 },
          resources: {
            inventories: [{ source_id: 10, name: 'InventoryA', type: 'inventory', action: 'create' }],
            organizations: [{ source_id: 1, name: 'Default', type: 'organization', action: 'skip' }],
          },
        }}
        exclude={{}}
        onExcludeChange={vi.fn()}
      />
    );

    expect(screen.getByText('Check credentials')).toBeInTheDocument();
    expect(
      screen.getByText((_, element) =>
        element?.textContent === '1 to create, 1 to skip (already exist)'
      )
    ).toBeInTheDocument();

    fireEvent.click(
      await screen.findByRole('button', { name: /Default Exclusions/i })
    );

    await waitFor(() => expect(screen.getByText('Organizations:')).toBeInTheDocument());
    expect(screen.getByText('Default')).toBeInTheDocument();
    expect(screen.getByText(/3 hosts, 1 groups total/)).toBeInTheDocument();
  });

  it('toggles full-type and item-level exclusions', () => {
    const onExcludeChange = vi.fn();

    render(
      <MigrationPreview
        preview={{
          source_id: 'src',
          destination_id: 'dst',
          warnings: [],
          resources: {
            organizations: [
              { source_id: 1, name: 'Default', type: 'organization', action: 'create' },
              { source_id: 2, name: 'Engineering', type: 'organization', action: 'skip' },
            ],
          },
        }}
        exclude={{}}
        onExcludeChange={onExcludeChange}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: /Organizations/i }));
    fireEvent.click(screen.getByLabelText(/Exclude all organizations/i));
    expect(onExcludeChange).toHaveBeenLastCalledWith({ organizations: ['1', '2'] });

    fireEvent.click(screen.getByLabelText(/Exclude Default/i));
    expect(onExcludeChange).toHaveBeenLastCalledWith({ organizations: ['1'] });
  });
});
