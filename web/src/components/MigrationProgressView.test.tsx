import { fireEvent, render, screen } from '@testing-library/react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@patternfly/react-core', () => ({
  Label: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Progress: ({ value }: { value: number }) => <div>progress:{Math.round(value)}</div>,
  ProgressMeasureLocation: { none: 'none' },
  Button: ({ children, onClick, ...props }: ButtonHTMLAttributes<HTMLButtonElement>) => (
    <button onClick={onClick} {...props}>
      {children}
    </button>
  ),
  Tooltip: ({ children }: { children: ReactNode }) => <>{children}</>,
  Modal: ({ children, title, isOpen, actions }: { children: ReactNode; title: string; isOpen: boolean; actions: ReactNode[] }) =>
    isOpen ? (
      <div>
        <h1>{title}</h1>
        {children}
        <div>{actions}</div>
      </div>
    ) : null,
  ModalVariant: { medium: 'medium' },
}));

vi.mock('@patternfly/react-icons/dist/esm/icons/check-circle-icon', () => ({ default: () => <span>check</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/exclamation-circle-icon', () => ({ default: () => <span>error</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/exclamation-triangle-icon', () => ({ default: () => <span>warn</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/in-progress-icon', () => ({ default: () => <span>progress</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/pending-icon', () => ({ default: () => <span>pending</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/angle-right-icon', () => ({ default: () => <span>right</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/angle-down-icon', () => ({ default: () => <span>down</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/angle-double-down-icon', () => ({ default: () => <span>ddown</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/angle-double-up-icon', () => ({ default: () => <span>dup</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/compress-icon', () => ({ default: () => <span>compress</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/expand-icon', () => ({ default: () => <span>expand</span> }));

import { MigrationProgressView } from './MigrationProgressView';

describe('MigrationProgressView', () => {
  beforeEach(() => {
    Element.prototype.scrollTo = vi.fn();
  });

  it('renders the appropriate empty state', () => {
    const { rerender } = render(<MigrationProgressView events={[]} jobStatus="running" />);
    expect(screen.getByText('Waiting for migration events...')).toBeInTheDocument();

    rerender(<MigrationProgressView events={[]} jobStatus="completed" />);
    expect(screen.getByText('No migration progress data available.')).toBeInTheDocument();
  });

  it('builds migration state from events and shows error details', () => {
    render(
      <MigrationProgressView
        jobStatus="running"
        events={[
          { _event: 'migration_start', total_phases: 2 },
          { _event: 'phase_start', phase_num: 1, total_phases: 2, description: 'Export Organizations' },
          { _event: 'resource_result', phase_num: 1, name: 'Default', resource_type: 'organizations', result: 'created', detail: 'created ok' },
          { _event: 'phase_progress', phase_num: 1, exported: 4, created: 1, skipped: 0, failed: 0, rate: '1/s', elapsed: '1s' },
          { _event: 'phase_complete', phase_num: 1, description: 'Export Organizations', created: 1, updated: 0, skipped: 0, failed: 0, exported: 4, duration: '2s', warnings: {} },
          { _event: 'phase_start', phase_num: 2, total_phases: 2, description: 'Import Credentials' },
          { _event: 'phase_error', phase_num: 2, error: 'credential mismatch' },
          { _event: 'migration_complete', total_created: 1, total_updated: 0, total_skipped: 0, total_failed: 1 },
        ]}
      />
    );

    expect(screen.getByText('Migration Output')).toBeInTheDocument();
    expect(screen.getByText('2/2 phases')).toBeInTheDocument();
    expect(screen.getByText('1 created')).toBeInTheDocument();
    expect(screen.getByText('1 failed')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    expect(screen.getByText('Default')).toBeInTheDocument();
    expect(screen.getByText('organizations')).toBeInTheDocument();

    fireEvent.click(screen.getByText('credential mismatch'));
    expect(screen.getByText('Error Details')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Collapse all' }));
    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));
    fireEvent.click(screen.getByRole('button', { name: 'Scroll to top' }));
    fireEvent.click(screen.getByRole('button', { name: 'Scroll to bottom' }));
    expect(Element.prototype.scrollTo).toHaveBeenCalled();
  });
});
