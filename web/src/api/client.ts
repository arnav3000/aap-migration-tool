const BASE = '';

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const opts: RequestInit = {
    method,
    headers: { 'Content-Type': 'application/json' },
  };
  if (body !== undefined) {
    opts.body = JSON.stringify(body);
  }
  const resp = await fetch(`${BASE}${path}`, opts);
  if (resp.status === 204) return undefined as T;
  const text = await resp.text();
  let data: Record<string, unknown>;
  try {
    data = JSON.parse(text);
  } catch {
    throw new Error(resp.ok ? 'Invalid JSON response' : `HTTP ${resp.status}: ${text.slice(0, 200)}`);
  }
  if (!resp.ok) {
    const msg = data.detail || data.error || `HTTP ${resp.status}`;
    throw new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
  }
  return data as T;
}

export const api = {
  createConnection: (conn: unknown) => request<unknown>('POST', '/api/connections', conn),
  listConnections: () => request<unknown[]>('GET', '/api/connections'),
  updateConnection: (id: string, conn: unknown) => request<unknown>('PUT', `/api/connections/${id}`, conn),
  deleteConnection: (id: string) => request<void>('DELETE', `/api/connections/${id}`),
  testConnection: (id: string) => request<{ ok: boolean; error?: string }>('POST', `/api/connections/${id}/test`),

  listResourceTypes: (connId: string) => request<unknown[]>('GET', `/api/connections/${connId}/resources`),
  listResources: (connId: string, type: string) => request<unknown[]>('GET', `/api/connections/${connId}/resources/${type}`),

  runCleanup: (connId: string) => request<{ job_id: string }>('POST', `/api/connections/${connId}/cleanup`),
  runExport: (connId: string) => request<{ job_id: string }>('POST', `/api/connections/${connId}/export`),
  selectiveMigrate: (sourceId: string, destinationId: string, jobTemplateIds: number[], forceUpdate = false) =>
    request<{ job_id: string }>('POST', '/api/selective-migrate', {
      source_id: sourceId,
      destination_id: destinationId,
      job_template_ids: jobTemplateIds,
      force_update: forceUpdate,
    }),

  migrationPreview: (sourceId: string, destinationId: string, organizations?: number[], namePrefix?: string) =>
    request<{ job_id: string }>('POST', '/api/migrate/preview', {
      source_id: sourceId,
      destination_id: destinationId,
      ...(organizations?.length ? { organizations } : {}),
      ...(namePrefix ? { name_prefix: namePrefix } : {}),
    }),
  getMigrationPreview: (jobId: string) =>
    request<unknown>('GET', `/api/migrate/preview/${jobId}`),
  migrationRun: (sourceId: string, destinationId: string, previewJobId: string, exclusions?: Record<string, string[]>, organizations?: number[], namePrefix?: string) => {
    const intExclusions: Record<string, number[]> = {};
    if (exclusions) {
      for (const [type, ids] of Object.entries(exclusions)) {
        intExclusions[type] = ids.map(id => parseInt(id, 10)).filter(n => !isNaN(n));
      }
    }
    return request<{ job_id: string }>('POST', '/api/migrate/run', {
      source_id: sourceId,
      destination_id: destinationId,
      job_id: previewJobId,
      exclusions: intExclusions,
      ...(organizations?.length ? { organizations } : {}),
      ...(namePrefix ? { name_prefix: namePrefix } : {}),
    });
  },

  listOrganizations: (connId: string) =>
    request<{ id: number; name: string; description: string }[]>('GET', `/api/connections/${connId}/organizations`),

  clearMigrationState: () => request<{ cleared_progress: number; deleted_mappings: number }>('POST', '/api/migrate/clear-state'),
  getConcurrency: () => request<{ max_concurrent: number }>('GET', '/api/settings/concurrency'),
  updateConcurrency: (maxConcurrent: number) => request<{ max_concurrent: number }>('PUT', '/api/settings/concurrency', { max_concurrent: maxConcurrent }),
  getExclusions: () => request<unknown>('GET', '/api/exclusions'),

  listJobs: () => request<unknown[]>('GET', '/api/jobs'),
  getJob: (id: string) => request<unknown>('GET', `/api/jobs/${id}`),
  cancelJob: (jobId: string) => request<{ status: string }>('POST', `/api/jobs/${jobId}/cancel`),
  resumeJob: (jobId: string) => request<{ status: string; new_job_id?: string }>('POST', `/api/jobs/${jobId}/resume`),
  getJobCredentialsCsvUrl: (jobId: string) => `/api/jobs/${jobId}/credentials.csv`,

  runAnalysis: (connectionId: string) =>
    request<{ job_id: string }>('POST', '/api/analysis/run', { connection_id: connectionId }),
  getAnalysisResult: (jobId: string) =>
    request<unknown>('GET', `/api/analysis/${jobId}`),
  exportAnalysisJson: (jobId: string) => `/api/analysis/${jobId}/export/json`,
  exportAnalysisHtml: (jobId: string) => `/api/analysis/${jobId}/export/html`,

  calculateSizing: (params: Record<string, unknown>) =>
    request<unknown>('POST', '/api/sizing/calculate', params),

  calculateDynamicSizing: (connectionId: string, historyDays: number = 30, deploymentTarget: string = 'ocp') =>
    request<unknown>('POST', '/api/sizing/dynamic', { connection_id: connectionId, history_days: historyDays, deployment_target: deploymentTarget }),

  // Migration Planner
  createPlan: (data: { name: string; description?: string; destination_id: string; sources?: { connection_id: string; name_prefix?: string; analysis_job_id?: string }[] }) =>
    request<unknown>('POST', '/api/plans', data),
  listPlans: () => request<unknown[]>('GET', '/api/plans'),
  getPlan: (id: string) => request<unknown>('GET', `/api/plans/${id}`),
  updatePlan: (id: string, data: Record<string, unknown>) => request<unknown>('PUT', `/api/plans/${id}`, data),
  deletePlan: (id: string) => request<void>('DELETE', `/api/plans/${id}`),
  updatePlanPhases: (id: string, data: { phases: unknown[]; sources?: unknown[] }) =>
    request<unknown>('PUT', `/api/plans/${id}/phases`, data),
  populatePlan: (id: string) => request<unknown>('POST', `/api/plans/${id}/populate`),
  executePlanPhase: (planId: string, phaseId: string) =>
    request<{ job_id: string }>('POST', `/api/plans/${planId}/phases/${phaseId}/execute`),

  listMigratableResourceTypes: () =>
    request<{ name: string; description: string; migration_order: number; dependencies: string[] }[]>('GET', '/api/resource-types'),

  // Validate — shared engine with CLI (validate/runner.py)
  runValidate: (params: { live?: boolean; resource_type?: string; skip_hosts?: boolean; organizations?: string[]; source_id?: string; destination_id?: string; output_dir?: string }) =>
    request<{ job_id: string }>('POST', '/api/validate/run', params),
  getValidateResult: (jobId: string) =>
    request<unknown>('GET', `/api/validate/${jobId}`),
  exportValidateJson: (jobId: string) => `/api/validate/${jobId}/export/json`,
  exportValidateHtml: (jobId: string) => `/api/validate/${jobId}/export/html`,

  // IAM — shared engine with CLI (iam/analyser.py)
  iamAudit: (params: { source_id: string; verify_ssl?: boolean; timeout?: number; workers?: number; scan_strategy?: 'resource' | 'principal'; resume?: boolean; checkpoint_dir?: string }) =>
    request<{ job_id: string }>('POST', '/api/iam/audit', params),
  iamMigrate: (params: { source_id: string; destination_id: string; state_db_path?: string; verify_ssl?: boolean; timeout?: number; workers?: number; scan_strategy?: 'resource' | 'principal'; dry_run?: boolean; skip_user_roles?: boolean; users_only?: boolean; resume?: boolean; checkpoint_dir?: string }) =>
    request<{ job_id: string }>('POST', '/api/iam/migrate', params),
  iamBenchmark: (params: { source_id: string; verify_ssl?: boolean; sample_size?: number; workers?: number[] }) =>
    request<{ source_id: string; output: string }>('POST', '/api/iam/benchmark', params),
  getIamResult: (jobId: string) =>
    request<unknown>('GET', `/api/iam/${jobId}`),
  exportIamJson: (jobId: string) => `/api/iam/${jobId}/export/json`,
  exportIamHtml: (jobId: string) => `/api/iam/${jobId}/export/html`,
};

export function createJobLogSocket(jobId: string, onMessage: (line: string) => void, onClose?: (status: string) => void): WebSocket {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const ws = new WebSocket(`${proto}//${window.location.host}/ws/jobs/${jobId}/logs`);
  ws.onmessage = (e) => onMessage(e.data);
  ws.onclose = (e) => onClose?.(e.reason || 'closed');
  return ws;
}
