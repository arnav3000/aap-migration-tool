import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Card,
  CardBody,
  CardTitle,
  Gallery,
  PageSection,
  Spinner,
  Title,
} from '@patternfly/react-core';
import { api } from '../api/client';
import type { HealthOut, JobListOut, MigrationStatusOut } from '../api/types';
import { StatusDot } from '../components/StatusBadge';

export function Dashboard() {
  const [health, setHealth] = useState<HealthOut | null>(null);
  const [status, setStatus] = useState<MigrationStatusOut | null>(null);
  const [recent, setRecent] = useState<JobListOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [h, s, j] = await Promise.all([
        api.get<HealthOut>('/health'),
        api.get<MigrationStatusOut>('/migrations/status'),
        api.get<JobListOut>('/jobs?limit=5&offset=0'),
      ]);
      setHealth(h);
      setStatus(s);
      setRecent(j);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load dashboard');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  return (
    <PageSection>
      <Title headingLevel="h1">Dashboard</Title>
      {loading && <Spinner aria-label="Loading dashboard" />}
      {error && (
        <Alert variant="danger" title="Could not reach the API" style={{ marginTop: 16 }}>
          {error}. Verify the API container is running and the API key is set.
        </Alert>
      )}
      {!loading && !error && (
        <Gallery hasGutter style={{ marginTop: 16 }}>
          <Card>
            <CardTitle>API health</CardTitle>
            <CardBody>
              <p>Status: {health?.status ?? 'unknown'}</p>
              <p>Version: {health?.version ?? '—'}</p>
              <p>Worker: {health?.worker ?? '—'}</p>
              <p>Queue depth: {health?.queue_depth ?? 0}</p>
            </CardBody>
          </Card>
          <Card>
            <CardTitle>Migration state</CardTitle>
            <CardBody>
              {status?.warning ? (
                <p>{status.warning}</p>
              ) : (
                <>
                  <p>Migration ID: {status?.migration_id ?? '—'}</p>
                  <p>Resource types tracked: {Object.keys(status?.resource_stats ?? {}).length}</p>
                  {(status?.unreadable_types?.length ?? 0) > 0 && (
                    <p>Unreadable: {status?.unreadable_types.join(', ')}</p>
                  )}
                </>
              )}
            </CardBody>
          </Card>
          <Card>
            <CardTitle>Recent jobs</CardTitle>
            <CardBody>
              {(recent?.items?.length ?? 0) === 0 && <p>No jobs yet.</p>}
              {(recent?.items ?? []).map((job) => (
                <p key={job.job_id}>
                  <StatusDot status={job.status} />
                  <Link to={`/jobs/${job.job_id}`}>
                    {job.job_type} · {job.job_id.slice(0, 8)}
                  </Link>{' '}
                  ({job.status})
                </p>
              ))}
              <p>
                <Link to="/jobs">View all jobs</Link>
              </p>
            </CardBody>
          </Card>
        </Gallery>
      )}
    </PageSection>
  );
}
