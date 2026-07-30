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
import type { MigrationState } from '../hooks/useJobLogs';

const EMPTY_STATE: MigrationState = {
  totalPhases: 0,
  phases: [],
  totalCreated: 0,
  totalUpdated: 0,
  totalSkipped: 0,
  totalFailed: 0,
  status: 'running',
  eventCount: 0,
};

describe('MigrationProgressView', () => {
  beforeEach(() => {
    Element.prototype.scrollTo = vi.fn();
  });

  it('renders the appropriate empty state', () => {
    const { rerender } = render(<MigrationProgressView migration={EMPTY_STATE} jobStatus="running" />);
    expect(screen.getByText('Waiting for migration events...')).toBeInTheDocument();

    rerender(<MigrationProgressView migration={EMPTY_STATE} jobStatus="completed" />);
    expect(screen.getByText('No migration progress data available.')).toBeInTheDocument();
  });

  it('builds migration state from events and shows error details', () => {
    const migration: MigrationState = {
      totalPhases: 2,
      totalCreated: 1,
      totalUpdated: 0,
      totalSkipped: 0,
      totalFailed: 1,
      status: 'failed',
      eventCount: 8,
      phases: [
        {
          num: 1,
          description: 'Export Organizations',
          status: 'complete',
          exported: 4,
          created: 1,
          updated: 0,
          skipped: 0,
          failed: 0,
          rate: '1/s',
          elapsed: '1s',
          duration: '2s',
          resources: [
            { name: 'Default', resourceType: 'organizations', result: 'created', detail: 'created ok' },
          ],
        },
        {
          num: 2,
          description: 'Import Credentials',
          status: 'failed',
          exported: 0,
          created: 0,
          updated: 0,
          skipped: 0,
          failed: 0,
          rate: '--/s',
          elapsed: '0s',
          duration: '',
          resources: [],
          error: 'credential mismatch',
        },
      ],
    };

    render(
      <MigrationProgressView
        jobStatus="running"
        migration={migration}
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
