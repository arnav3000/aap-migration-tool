import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Button,
  PageSection,
  Pagination,
  Select,
  SelectList,
  SelectOption,
  MenuToggle,
  Spinner,
  Title,
  Toolbar,
  ToolbarContent,
  ToolbarItem,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { api } from '../api/client';
import type { JobListOut, JobStatusValue } from '../api/types';
import { StatusDot } from '../components/StatusBadge';

const PAGE_SIZE = 20;

export function Jobs() {
  const [statusFilter, setStatusFilter] = useState<JobStatusValue | ''>('');
  const [filterOpen, setFilterOpen] = useState(false);
  const [page, setPage] = useState(1);
  const [data, setData] = useState<JobListOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const qs = new URLSearchParams({
        limit: String(PAGE_SIZE),
        offset: String((page - 1) * PAGE_SIZE),
      });
      if (statusFilter) {
        qs.set('status', statusFilter);
      }
      const result = await api.get<JobListOut>(`/jobs?${qs.toString()}`);
      setData(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load jobs');
    } finally {
      setLoading(false);
    }
  }, [page, statusFilter]);

  useEffect(() => {
    void load();
  }, [load]);

  // Auto-refresh the list while any visible job is active (AWX-style live list).
  useEffect(() => {
    const anyActive = (data?.items ?? []).some(
      (j) => j.status === 'queued' || j.status === 'running',
    );
    if (!anyActive) {
      return;
    }
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, [data, load]);

  return (
    <PageSection>
      <Title headingLevel="h1">Jobs</Title>
      <Toolbar>
        <ToolbarContent>
          <ToolbarItem>
            <Select
              isOpen={filterOpen}
              selected={statusFilter || 'all'}
              onSelect={(_e, value) => {
                setStatusFilter(value === 'all' ? '' : (value as JobStatusValue));
                setPage(1);
                setFilterOpen(false);
              }}
              onOpenChange={setFilterOpen}
              toggle={(toggleRef) => (
                <MenuToggle
                  ref={toggleRef}
                  onClick={() => setFilterOpen(!filterOpen)}
                  isExpanded={filterOpen}
                >
                  {statusFilter || 'All statuses'}
                </MenuToggle>
              )}
            >
              <SelectList>
                <SelectOption value="all">All statuses</SelectOption>
                <SelectOption value="queued">Queued</SelectOption>
                <SelectOption value="running">Running</SelectOption>
                <SelectOption value="succeeded">Successful</SelectOption>
                <SelectOption value="failed">Failed</SelectOption>
                <SelectOption value="cancelled">Canceled</SelectOption>
              </SelectList>
            </Select>
          </ToolbarItem>
          <ToolbarItem>
            <Button variant="link" onClick={() => load()}>
              Refresh
            </Button>
          </ToolbarItem>
          <ToolbarItem align={{ default: 'alignEnd' }}>
            <Pagination
              itemCount={data?.total ?? 0}
              perPage={PAGE_SIZE}
              page={page}
              onSetPage={(_e, p) => setPage(p)}
              isCompact
            />
          </ToolbarItem>
        </ToolbarContent>
      </Toolbar>
      {loading && <Spinner aria-label="Loading jobs" />}
      {error && <Alert variant="danger" title={error} style={{ marginTop: 16 }} />}
      {!loading && !error && (
        <Table aria-label="Jobs">
          <Thead>
            <Tr>
              <Th>Status</Th>
              <Th>Type</Th>
              <Th>ID</Th>
              <Th>Updated</Th>
            </Tr>
          </Thead>
          <Tbody>
            {(data?.items ?? []).map((job) => (
              <Tr key={job.job_id}>
                <Td>
                  <StatusDot status={job.status} />
                  {job.status}
                </Td>
                <Td>{job.job_type}</Td>
                <Td>
                  <Link to={`/jobs/${job.job_id}`}>{job.job_id.slice(0, 8)}…</Link>
                </Td>
                <Td>{job.updated_at ?? '—'}</Td>
              </Tr>
            ))}
          </Tbody>
        </Table>
      )}
      {!loading && !error && (data?.items?.length ?? 0) === 0 && <p>No jobs found.</p>}
    </PageSection>
  );
}
