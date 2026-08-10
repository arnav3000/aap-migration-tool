export interface Connection {
  id: string;
  name: string;
  type: 'awx' | 'aap';
  role: 'source' | 'destination' | 'target';
  url: string;
  token?: string;
  has_token?: boolean;
  verify_ssl: boolean;
  version?: string;
  api_prefix?: string;
  ping_status?: 'unknown' | 'ok' | 'error';
  ping_error?: string;
  auth_status?: 'unknown' | 'ok' | 'error';
  auth_error?: string;
  last_checked?: string;
}

export interface TestResult {
  ok: boolean;
  error?: string;
}
