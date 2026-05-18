import { renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi, beforeEach } from 'vitest';

import { useJobLogs } from './useJobLogs';
import { api, createJobLogSocket } from '../api/client';

vi.mock('../api/client', () => ({
  api: {
    getJob: vi.fn(),
  },
  createJobLogSocket: vi.fn(),
}));

describe('useJobLogs', () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it('returns empty status when no job id is provided', () => {
    const { result } = renderHook(() => useJobLogs(''));

    expect(result.current.status).toBe('empty');
    expect(result.current.textLines).toEqual([]);
    expect(result.current.events).toEqual([]);
  });

  it('separates websocket text lines from event messages', async () => {
    const close = vi.fn();
    let onMessage: ((line: string) => void) | undefined;

    vi.mocked(createJobLogSocket).mockImplementation((_, handleMessage) => {
      onMessage = handleMessage;
      return { close } as unknown as WebSocket;
    });

    const { result } = renderHook(() => useJobLogs('job-1'));

    onMessage?.('plain line');
    onMessage?.('\t{"_event":"phase_start","phase_num":1,"total_phases":2,"description":"Phase 1"}');

    await waitFor(() => expect(result.current.status).toBe('streaming'));
    expect(result.current.textLines).toEqual(['plain line']);
    expect(result.current.events).toEqual([
      { _event: 'phase_start', phase_num: 1, total_phases: 2, description: 'Phase 1' },
    ]);
  });

  it('falls back to REST when websocket closes without data', async () => {
    let onClose: ((reason: string) => void) | undefined;

    vi.mocked(createJobLogSocket).mockImplementation((_, _handleMessage, handleClose) => {
      onClose = handleClose;
      return { close: vi.fn() } as unknown as WebSocket;
    });
    vi.mocked(api.getJob).mockResolvedValue({
      status: 'completed',
      output: [
        'plain line',
        '\t{"_event":"phase_complete","phase_num":1,"description":"Phase 1","created":1,"updated":0,"skipped":0,"failed":0,"exported":1,"duration":"1s","warnings":{}}',
      ],
    });

    const { result } = renderHook(() => useJobLogs('job-2'));
    onClose?.('completed');

    await waitFor(() => expect(result.current.status).toBe('completed'));
    expect(result.current.textLines).toEqual(['plain line']);
    expect(result.current.events[0]).toMatchObject({ _event: 'phase_complete' });
  });

  it('uses metadata events when job output has no event lines and handles rest failures', async () => {
    let onClose: ((reason: string) => void) | undefined;

    vi.mocked(createJobLogSocket).mockImplementation((_, _handleMessage, handleClose) => {
      onClose = handleClose;
      return { close: vi.fn() } as unknown as WebSocket;
    });

    vi.mocked(api.getJob)
      .mockResolvedValueOnce({
        status: 'failed',
        output: ['plain'],
        job_metadata: {
          events: [{ _event: 'migration_complete', total_created: 0, total_updated: 0, total_skipped: 0, total_failed: 1 }],
        },
      })
      .mockRejectedValueOnce(new Error('boom'));

    const first = renderHook(() => useJobLogs('job-3'));
    onClose?.('failed');
    await waitFor(() => expect(first.result.current.status).toBe('failed'));
    expect(first.result.current.events).toEqual([
      { _event: 'migration_complete', total_created: 0, total_updated: 0, total_skipped: 0, total_failed: 1 },
    ]);

    const second = renderHook(() => useJobLogs('job-4'));
    onClose?.('closed');
    await waitFor(() => expect(second.result.current.status).toBe('error'));
  });
});
