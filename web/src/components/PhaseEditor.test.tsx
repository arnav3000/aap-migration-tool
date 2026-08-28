import { fireEvent, render, screen } from '@testing-library/react';
import type { ButtonHTMLAttributes, InputHTMLAttributes, ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';
import type { PlanPhase } from '../types/resources';

vi.mock('@patternfly/react-core', () => ({
  Button: ({ children, onClick, isDisabled, ...props }: ButtonHTMLAttributes<HTMLButtonElement> & { isDisabled?: boolean }) => (
    <button disabled={isDisabled} onClick={onClick} {...props}>
      {children}
    </button>
  ),
  Card: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardBody: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardHeader: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  CardTitle: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Text: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Label: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Split: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  SplitItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  Flex: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  FlexItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  TextInput: ({ value, onChange, onBlur, onKeyDown, autoFocus }: InputHTMLAttributes<HTMLInputElement> & { value: string; onChange: (_e: unknown, value: string) => void }) => (
    <input
      aria-label="phase-name"
      value={value}
      autoFocus={autoFocus}
      onChange={(event) => onChange(event, event.currentTarget.value)}
      onBlur={onBlur}
      onKeyDown={onKeyDown}
    />
  ),
  Alert: ({ title }: { title: string }) => <div>{title}</div>,
}));

vi.mock('@patternfly/react-icons/dist/esm/icons/angle-up-icon', () => ({ default: () => <span>up</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/angle-down-icon', () => ({ default: () => <span>down</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/plus-circle-icon', () => ({ default: () => <span>plus</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/times-icon', () => ({ default: () => <span>times</span> }));
vi.mock('@patternfly/react-icons/dist/esm/icons/exclamation-triangle-icon', () => ({ default: () => <span>warn</span> }));

import { PhaseEditor } from './PhaseEditor';

describe('PhaseEditor', () => {
  it('shows an empty-state alert and can add a phase', () => {
    const onChange = vi.fn();

    render(
      <PhaseEditor
        phases={[]}
        sources={[]}
        sourceNames={{}}
        onChange={onChange}
      />
    );

    expect(screen.getByText(/No phases yet/i)).toBeInTheDocument();
    fireEvent.click(screen.getByText('Add Phase'));

    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0][0]).toMatchObject({
      phase_number: 1,
      name: 'Phase 1',
      orgs: [],
    });
  });

  it('edits names, moves orgs, removes orgs and surfaces dependency warnings', () => {
    const onChange = vi.fn();
    const phases: PlanPhase[] = [
      {
        id: 'phase-1',
        phase_number: 1,
        name: 'Phase One',
        status: 'pending',
        update_mode: false,
        resource_types: [],
        orgs: [{ id: 'org-1', source_id: 'src-1', org_id: 1, org_name: 'Default' }],
      },
      {
        id: 'phase-2',
        phase_number: 2,
        name: 'Phase Two',
        status: 'pending',
        update_mode: false,
        resource_types: [],
        orgs: [{ id: 'org-2', source_id: 'src-1', org_id: 2, org_name: 'Engineering' }],
      },
    ];

    render(
      <PhaseEditor
        phases={phases}
        sources={[{ id: 'src-1', connection_id: 'conn-1', analysis_job_id: 'analysis-1' }]}
        sourceNames={{ 'src-1': 'Source Alpha' }}
        analysisData={{
          'analysis-1': {
            organizations: {
              Default: {
                org_id: 1,
                can_migrate_standalone: false,
                required_migrations_before: ['Engineering'],
                dependencies: {
                  Engineering: [
                    { resource_type: 'credential', resource_name: 'Vault', required_by: ['Default'] },
                  ],
                },
              },
            },
          },
        }}
        onChange={onChange}
      />
    );

    expect(screen.getByText(/Depends on "Engineering" which is in a later phase/)).toBeInTheDocument();
    expect(screen.getByText(/Uses credential:Vault from "Engineering"/)).toBeInTheDocument();

    fireEvent.click(screen.getByText('Phase One'));
    fireEvent.change(screen.getByLabelText('phase-name'), { target: { value: 'Renamed Phase' } });
    fireEvent.blur(screen.getByLabelText('phase-name'));

    expect(onChange).toHaveBeenCalledWith([
      expect.objectContaining({ id: 'phase-1', name: 'Renamed Phase' }),
      expect.objectContaining({ id: 'phase-2' }),
    ]);

    fireEvent.click(screen.getAllByLabelText('Move down')[0]);
    expect(onChange).toHaveBeenCalledWith([
      expect.objectContaining({ id: 'phase-1', orgs: [] }),
      expect.objectContaining({
        id: 'phase-2',
        orgs: expect.arrayContaining([expect.objectContaining({ id: 'org-1' })]),
      }),
    ]);

    fireEvent.click(screen.getAllByLabelText('Remove')[0]);
    expect(onChange).toHaveBeenCalledWith([
      expect.objectContaining({ orgs: [] }),
      expect.objectContaining({ orgs: expect.any(Array) }),
    ]);
  });
});
