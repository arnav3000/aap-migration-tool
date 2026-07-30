import { fireEvent, render, screen } from '@testing-library/react';
import type { ReactNode } from 'react';
import { describe, expect, it, vi } from 'vitest';

vi.mock('@patternfly/react-core', () => ({
  SearchInput: ({
    value,
    onChange,
    onClear,
    placeholder,
  }: {
    value: string;
    onChange: (_e: unknown, value: string) => void;
    onClear: () => void;
    placeholder?: string;
  }) => (
    <div>
      <input
        aria-label={placeholder ?? 'search'}
        value={value}
        onChange={(event) => onChange(event, event.currentTarget.value)}
      />
      <button onClick={onClear}>clear</button>
    </div>
  ),
  Toolbar: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ToolbarItem: ({ children }: { children: ReactNode }) => <div>{children}</div>,
  ToolbarContent: ({ children }: { children: ReactNode }) => <div>{children}</div>,
}));

vi.mock('@patternfly/react-table', () => ({
  Table: ({ children }: { children: ReactNode }) => <table>{children}</table>,
  Thead: ({ children }: { children: ReactNode }) => <thead>{children}</thead>,
  Tbody: ({ children }: { children: ReactNode }) => <tbody>{children}</tbody>,
  Tr: ({ children }: { children: ReactNode }) => <tr>{children}</tr>,
  Th: ({ children }: { children: ReactNode }) => <th>{children}</th>,
  Td: ({ children }: { children: ReactNode }) => <td>{children}</td>,
}));

import { ResourceTable } from './ResourceTable';

describe('ResourceTable', () => {
  it('renders prioritized columns and filters by name', () => {
    render(
      <ResourceTable
        resources={[
          { id: 1, name: 'Alpha', description: 'one', metadata: { ignored: true }, extra: 'x' },
          { id: 2, name: 'Beta', description: 'two', extra: 'y' },
        ]}
      />
    );

    expect(screen.getByText('id')).toBeInTheDocument();
    expect(screen.getByText('name')).toBeInTheDocument();
    expect(screen.getByText('description')).toBeInTheDocument();
    expect(screen.queryByText('metadata')).not.toBeInTheDocument();
    expect(screen.getByText('2 of 2 items')).toBeInTheDocument();

    fireEvent.change(screen.getByLabelText(/Filter by name/i), { target: { value: 'beta' } });
    expect(screen.queryByText('Alpha')).not.toBeInTheDocument();
    expect(screen.getByText('Beta')).toBeInTheDocument();
    expect(screen.getByText('1 of 2 items')).toBeInTheDocument();

    fireEvent.click(screen.getByText('clear'));
    expect(screen.getByText('Alpha')).toBeInTheDocument();
  });

  it('handles empty resources and formats primitive cells', () => {
    const { rerender, container } = render(<ResourceTable resources={[]} />);
    expect(screen.getByText('0 of 0 items')).toBeInTheDocument();

    rerender(
      <ResourceTable
        resources={[{ id: 3, username: 'alice', status: true, organization: null }]}
      />
    );

    expect(screen.getByText('alice')).toBeInTheDocument();
    expect(screen.getByText('true')).toBeInTheDocument();
    expect(container.querySelectorAll('td').length).toBeGreaterThan(0);
  });
});
