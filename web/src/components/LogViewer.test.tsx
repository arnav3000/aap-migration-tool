import { fireEvent, render, screen } from '@testing-library/react';
import type { ButtonHTMLAttributes, ReactNode } from 'react';
import { describe, expect, it } from 'vitest';
import { vi } from 'vitest';

vi.mock('@patternfly/react-core', () => ({
  Button: ({
    children,
    isDisabled,
    ...props
  }: ButtonHTMLAttributes<HTMLButtonElement> & { isDisabled?: boolean }) => (
    <button disabled={isDisabled} {...props}>
      {children}
    </button>
  ),
  Label: ({ children }: { children: ReactNode }) => <span>{children}</span>,
  Tooltip: ({ children }: { children: ReactNode }) => <>{children}</>,
  SearchInput: ({
    value,
    onChange,
    placeholder,
  }: {
    value: string;
    onChange: (_event: unknown, value: string) => void;
    placeholder?: string;
  }) => (
    <input
      aria-label={placeholder ?? 'search'}
      value={value}
      onChange={(event) => onChange(event, event.currentTarget.value)}
    />
  ),
}));

vi.mock('@patternfly/react-icons/dist/esm/icons/angle-double-down-icon', () => ({
  default: () => <span>down</span>,
}));
vi.mock('@patternfly/react-icons/dist/esm/icons/angle-double-up-icon', () => ({
  default: () => <span>up</span>,
}));
vi.mock('@patternfly/react-icons/dist/esm/icons/play-icon', () => ({
  default: () => <span>play</span>,
}));
vi.mock('@patternfly/react-icons/dist/esm/icons/pause-icon', () => ({
  default: () => <span>pause</span>,
}));
vi.mock('@patternfly/react-icons/dist/esm/icons/download-icon', () => ({
  default: () => <span>download</span>,
}));
vi.mock('@patternfly/react-icons/dist/esm/icons/search-icon', () => ({
  default: () => <span>search</span>,
}));

vi.mock('../api/client', () => ({
  api: {
    getJob: vi.fn(),
  },
  createJobLogSocket: vi.fn(),
}));

import { api, createJobLogSocket } from '../api/client';
import { LogViewer } from './LogViewer';

describe('LogViewer', () => {
  it('renders external log lines and status summary', () => {
    render(
      <LogViewer
        jobId="job-1"
        externalStatus="completed"
        externalLines={['line 1', 'line 2']}
      />
    );

    expect(screen.getByText('Completed')).toBeInTheDocument();
    expect(screen.getByText('2 lines')).toBeInTheDocument();
    expect(screen.getByText('line 1')).toBeInTheDocument();
    expect(screen.getByText('line 2')).toBeInTheDocument();
  });

  it('collapses and expands detected log sections', () => {
    render(
      <LogViewer
        jobId="job-2"
        externalStatus="completed"
        externalLines={[
          '### Phase 1',
          'phase 1 detail',
          '### Phase 2',
          'phase 2 detail',
        ]}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: 'Collapse all' }));

    expect(screen.queryByText('phase 1 detail')).not.toBeInTheDocument();
    expect(screen.queryByText('phase 2 detail')).not.toBeInTheDocument();
    expect(screen.getAllByText('··· collapsed ···')).toHaveLength(2);

    fireEvent.click(screen.getByRole('button', { name: 'Expand all' }));

    expect(screen.getByText('phase 1 detail')).toBeInTheDocument();
    expect(screen.getByText('phase 2 detail')).toBeInTheDocument();
  });

  it('loads output from REST when websocket closes without data', async () => {
    let closeHandler: ((status: string) => void) | undefined;
    const onClose = vi.fn();

    vi.mocked(createJobLogSocket).mockImplementation((_jobId, _onMessage, onSocketClose) => {
      closeHandler = onSocketClose;
      return { close: vi.fn() } as unknown as WebSocket;
    });
    vi.mocked(api.getJob).mockResolvedValue({
      status: 'completed',
      output: ['rest line'],
    });

    render(<LogViewer jobId="job-rest" onClose={onClose} />);
    closeHandler?.('completed');

    expect(await screen.findByText('rest line')).toBeInTheDocument();
    expect(onClose).toHaveBeenCalledWith('completed');
    expect(screen.getByText('Completed')).toBeInTheDocument();
  });

  it('supports search, follow toggle, download, and empty states', () => {
    const createObjectURL = vi.fn().mockReturnValue('blob:logs');
    const revokeObjectURL = vi.fn();
    Object.defineProperty(URL, 'createObjectURL', { value: createObjectURL, writable: true });
    Object.defineProperty(URL, 'revokeObjectURL', { value: revokeObjectURL, writable: true });
    const click = vi.fn();
    const originalCreateElement = document.createElement.bind(document);
    vi.spyOn(document, 'createElement').mockImplementation((tagName: string) => {
      if (tagName === 'a') {
        return { click, href: '', download: '' } as unknown as HTMLAnchorElement;
      }
      return originalCreateElement(tagName);
    });

    const { rerender } = render(
      <LogViewer
        jobId="job-3"
        externalStatus="streaming"
        externalLines={['### Header', 'find me', 'other line']}
      />
    );

    fireEvent.click(screen.getByRole('button', { name: 'Toggle search' }));
    fireEvent.change(screen.getByLabelText('Search output...'), { target: { value: 'find' } });
    expect(screen.getByText('find me')).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Pause follow' }));
    expect(screen.getByRole('button', { name: 'Follow' })).toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    expect(createObjectURL).toHaveBeenCalled();
    expect(click).toHaveBeenCalled();
    expect(revokeObjectURL).toHaveBeenCalledWith('blob:logs');

    rerender(<LogViewer jobId="job-4" externalStatus="completed" externalLines={[]} />);
    expect(screen.getByText('No output available.')).toBeInTheDocument();
  });
});
