// Minimal fetch client for the AAP Bridge REST API (/api/v1).
// Auth: X-API-Key header from localStorage (set on the login/setup screen,
// mirrors AAP_BRIDGE_API_TOKEN). All calls go same-origin so the nginx
// reverse proxy (container) or Vite proxy (dev) forwards to FastAPI.

import type { ApiError } from './types';

const API_BASE = import.meta.env.VITE_API_BASE || '/api/v1';

export function getApiKey(): string {
  return localStorage.getItem('aap-bridge-api-key') || '';
}

export function setApiKey(key: string): void {
  if (key) {
    localStorage.setItem('aap-bridge-api-key', key);
  } else {
    localStorage.removeItem('aap-bridge-api-key');
  }
}

export class ApiHttpError extends Error {
  status: number;
  detail: string;

  constructor(status: number, detail: string) {
    super(`${status}: ${detail}`);
    this.status = status;
    this.detail = detail;
  }
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(init.headers as Record<string, string> | undefined),
  };
  const key = getApiKey();
  if (key) {
    headers['X-API-Key'] = key;
  }
  const res = await fetch(`${API_BASE}${path}`, { ...init, headers });
  if (res.status === 204) {
    return undefined as T;
  }
  const text = await res.text();
  const data = text ? (JSON.parse(text) as unknown) : undefined;
  if (!res.ok) {
    const detail =
      data && typeof (data as ApiError).detail === 'string'
        ? (data as ApiError).detail
        : res.statusText || 'Request failed';
    throw new ApiHttpError(res.status, detail);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>(path),
  post: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'POST', body: JSON.stringify(body ?? {}) }),
  put: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PUT', body: JSON.stringify(body ?? {}) }),
  patch: <T>(path: string, body: unknown) =>
    request<T>(path, { method: 'PATCH', body: JSON.stringify(body ?? {}) }),
  del: <T>(path: string) => request<T>(path, { method: 'DELETE' }),
};

/** Download a job artifact as a blob (uses fetch so X-API-Key is sent). */
export async function downloadArtifact(jobId: string, artifactPath: string): Promise<void> {
  const key = getApiKey();
  const res = await fetch(`${API_BASE}/jobs/${jobId}/artifacts/${artifactPath}`, {
    headers: key ? { 'X-API-Key': key } : {},
  });
  if (!res.ok) {
    throw new ApiHttpError(res.status, res.statusText || 'Download failed');
  }
  const blob = await res.blob();
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = artifactPath.split('/').pop() || 'artifact';
  document.body.appendChild(a);
  a.click();
  a.remove();
  URL.revokeObjectURL(url);
}
