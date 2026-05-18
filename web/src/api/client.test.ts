import { api, createJobLogSocket } from './client';
import { vi, describe, it, expect, beforeEach } from 'vitest';

describe('api client', () => {
  beforeEach(() => {
    vi.restoreAllMocks();
  });

  it('returns parsed JSON for successful requests', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 200,
        text: async () => JSON.stringify([{ id: 'job-1' }]),
      } satisfies Partial<Response>)
    );

    await expect(api.listJobs()).resolves.toEqual([{ id: 'job-1' }]);
  });

  it('serializes request bodies and filters invalid exclusion ids', async () => {
    const fetchMock = vi.fn().mockResolvedValue({
      ok: true,
      status: 200,
      text: async () => JSON.stringify({ job_id: 'job-1' }),
    } satisfies Partial<Response>);

    vi.stubGlobal('fetch', fetchMock);

    await expect(
      api.migrationRun('src', 'dst', 'preview-1', { inventories: ['1', 'x', '2'] }, [7], 'copy-')
    ).resolves.toEqual({ job_id: 'job-1' });

    expect(fetchMock).toHaveBeenCalledWith(
      '/api/migrate/run',
      expect.objectContaining({
        method: 'POST',
        body: JSON.stringify({
          source_id: 'src',
          destination_id: 'dst',
          job_id: 'preview-1',
          exclusions: { inventories: [1, 2] },
          organizations: [7],
          name_prefix: 'copy-',
        }),
      })
    );
  });

  it('returns undefined for 204 responses', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: true,
        status: 204,
        text: async () => '',
      } satisfies Partial<Response>)
    );

    await expect(api.deleteConnection('abc')).resolves.toBeUndefined();
  });

  it('uses API detail messages for JSON error payloads', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 400,
        text: async () => JSON.stringify({ detail: 'Invalid input' }),
      } satisfies Partial<Response>)
    );

    await expect(api.listConnections()).rejects.toThrow('Invalid input');
  });

  it('uses error payloads and throws on invalid success JSON', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn()
        .mockResolvedValueOnce({
          ok: false,
          status: 400,
          text: async () => JSON.stringify({ error: ['bad', 'news'] }),
        } satisfies Partial<Response>)
        .mockResolvedValueOnce({
          ok: true,
          status: 200,
          text: async () => 'not-json',
        } satisfies Partial<Response>)
    );

    await expect(api.listConnections()).rejects.toThrow('["bad","news"]');
    await expect(api.listJobs()).rejects.toThrow('Invalid JSON response');
  });

  it('surfaces non-JSON error payloads with HTTP status', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn().mockResolvedValue({
        ok: false,
        status: 502,
        text: async () => 'bad gateway',
      } satisfies Partial<Response>)
    );

    await expect(api.listConnections()).rejects.toThrow('HTTP 502: bad gateway');
  });

  it('creates a job log websocket and forwards events', () => {
    const received: string[] = [];
    const closed: string[] = [];

    class FakeWebSocket {
      static lastInstance: FakeWebSocket | undefined;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      url: string;

      constructor(url: string) {
        this.url = url;
        FakeWebSocket.lastInstance = this;
      }
    }

    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket);

    const socket = createJobLogSocket(
      'job-123',
      (line) => received.push(line),
      (status) => closed.push(status)
    );

    const instance = FakeWebSocket.lastInstance;
    expect(instance?.url).toContain('/ws/jobs/job-123/logs');
    expect(instance?.url.startsWith('ws://') || instance?.url.startsWith('wss://')).toBe(true);

    instance?.onmessage?.({ data: 'hello' } as MessageEvent);
    instance?.onclose?.({ reason: 'completed' } as CloseEvent);

    expect(received).toEqual(['hello']);
    expect(closed).toEqual(['completed']);
    expect(socket).toBe(instance);
  });

  it('exposes helper urls and uses close reason fallback', () => {
    const closed: string[] = [];

    class FakeWebSocket {
      static lastInstance: FakeWebSocket | undefined;
      onmessage: ((event: MessageEvent) => void) | null = null;
      onclose: ((event: CloseEvent) => void) | null = null;
      url: string;

      constructor(url: string) {
        this.url = url;
        FakeWebSocket.lastInstance = this;
      }
    }

    vi.stubGlobal('WebSocket', FakeWebSocket as unknown as typeof WebSocket);
    Object.defineProperty(window, 'location', {
      value: { protocol: 'https:', host: 'example.test' },
      writable: true,
    });

    createJobLogSocket('job-9', () => undefined, (status) => closed.push(status));
    FakeWebSocket.lastInstance?.onclose?.({ reason: '' } as CloseEvent);

    expect(FakeWebSocket.lastInstance?.url).toBe('wss://example.test/ws/jobs/job-9/logs');
    expect(closed).toEqual(['closed']);
    expect(api.getJobCredentialsCsvUrl('job-9')).toBe('/api/jobs/job-9/credentials.csv');
    expect(api.exportAnalysisJson('job-9')).toBe('/api/analysis/job-9/export/json');
    expect(api.exportAnalysisHtml('job-9')).toBe('/api/analysis/job-9/export/html');
  });
});
