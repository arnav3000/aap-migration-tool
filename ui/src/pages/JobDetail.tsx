import { useCallback, useEffect, useState } from 'react';
import { useNavigate, useParams } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  DescriptionList,
  DescriptionListTerm,
  DescriptionListGroup,
  DescriptionListDescription,
  Grid,
  GridItem,
  PageSection,
  Spinner,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { api, downloadArtifact } from '../api/client';
import type { JobArtifactsOut, JobCreated, JobStatus } from '../api/types';
import { JobStatusHeader } from '../components/awx/JobStatusHeader';
import { JobOutput } from '../components/JobOutput';
import { PageHeader } from '../components/Layout';

const CHAIN_ACTIONS: { label: string; path: string; buildBody: (jobId: string) => object }[] = [
  { label: 'Transform (chained)', path: '/transforms', buildBody: (jobId) => ({ job_id: jobId }) },
  { label: 'Import (chained)', path: '/imports', buildBody: (jobId) => ({ job_id: jobId }) },
  {
    label: 'Validate (chained)',
    path: '/validations/run',
    buildBody: (jobId) => ({ job_id: jobId }),
  },
  {
    label: 'Migration report (chained)',
    path: '/reports/migration',
    buildBody: (jobId) => ({ job_id: jobId }),
  },
];

export function JobDetail() {
  const { jobId } = useParams<{ jobId: string }>();
  const navigate = useNavigate();
  const [job, setJob] = useState<JobStatus | null>(null);
  const [artifacts, setArtifacts] = useState<JobArtifactsOut | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    if (!jobId) {
      return;
    }
    try {
      const [status, arts] = await Promise.all([
        api.get<JobStatus>(`/jobs/${jobId}`),
        api.get<JobArtifactsOut>(`/jobs/${jobId}/artifacts?limit=500&offset=0`),
      ]);
      setJob(status);
      setArtifacts(arts);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load job');
    }
  }, [jobId]);

  useEffect(() => {
    void load();
  }, [load]);

  // Live-poll while the job is active (AWX Job Output follows stdout).
  useEffect(() => {
    if (!job || (job.status !== 'queued' && job.status !== 'running')) {
      return;
    }
    const timer = window.setInterval(load, 3000);
    return () => window.clearInterval(timer);
  }, [job, load]);

  const runChainAction = async (label: string, path: string, body: object) => {
    setActionBusy(label);
    setError(null);
    setNotice(null);
    try {
      const created = await api.post<JobCreated>(path, body);
      setNotice(`${label} submitted as ${created.job_id}`);
      navigate(`/jobs/${created.job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : `${label} failed`);
    } finally {
      setActionBusy(null);
    }
  };

  const cancel = async () => {
    if (!jobId) {
      return;
    }
    setActionBusy('Cancel');
    try {
      await api.post(`/jobs/${jobId}/cancel`, {});
      await load();
      setNotice('Cancel requested — re-poll until the terminal status lands.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Cancel failed');
    } finally {
      setActionBusy(null);
    }
  };

  if (!jobId) {
    return (
      <PageSection>
        <Alert variant="warning" title="No job selected" />
      </PageSection>
    );
  }

  return (
    <PageSection>
      <PageHeader backTo="/jobs" backLabel="Back to Jobs" title="Job" />
      {error && <Alert variant="danger" title={error} style={{ marginTop: 16 }} />}
      {notice && <Alert variant="success" title={notice} style={{ marginTop: 16 }} />}
      {!job && !error && <Spinner aria-label="Loading job" />}
      {job && (
        <>
          <div style={{ margin: '16px 0' }}>
            <JobStatusHeader job={job} />
          </div>
          {job.error && (
            <Alert variant="danger" title="Error" style={{ marginBottom: 16 }} isInline>
              {job.error}
            </Alert>
          )}
          {typeof job.result?.message === 'string' && job.result.message && (
            <Alert variant="success" title="Result" style={{ marginBottom: 16 }} isInline>
              {job.result.message}
            </Alert>
          )}
          <JobOutput jobId={jobId} jobStatus={job.status} />

          <Grid hasGutter style={{ marginTop: 16 }}>
            <GridItem span={12} xl={6}>
              <Card isFullHeight>
                <CardTitle>Next phase (chain onto this job&apos;s directory)</CardTitle>
                <CardBody>
                  <div style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
                    {(job.status === 'queued' || job.status === 'running') && (
                      <Button
                        variant="danger"
                        onClick={cancel}
                        isDisabled={actionBusy !== null}
                      >
                        Cancel
                      </Button>
                    )}
                    {CHAIN_ACTIONS.map((action) => (
                      <Button
                        key={action.label}
                        variant="secondary"
                        isLoading={actionBusy === action.label}
                        isDisabled={actionBusy !== null}
                        onClick={() => runChainAction(action.label, action.path, action.buildBody(jobId))}
                      >
                        {action.label}
                      </Button>
                    ))}
                    <Button variant="link" onClick={() => load()}>
                      Refresh
                    </Button>
                  </div>
                  <DescriptionList style={{ marginTop: 12 }}>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Job ID</DescriptionListTerm>
                      <DescriptionListDescription>{job.job_id}</DescriptionListDescription>
                    </DescriptionListGroup>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Created</DescriptionListTerm>
                      <DescriptionListDescription>{job.created_at ?? '—'}</DescriptionListDescription>
                    </DescriptionListGroup>
                    <DescriptionListGroup>
                      <DescriptionListTerm>Updated</DescriptionListTerm>
                      <DescriptionListDescription>{job.updated_at ?? '—'}</DescriptionListDescription>
                    </DescriptionListGroup>
                  </DescriptionList>
                </CardBody>
              </Card>
            </GridItem>
            <GridItem span={12} xl={6}>
              <Card isFullHeight>
                <CardTitle>
                  Artifacts ({artifacts?.total ?? 0}
                  {artifacts?.truncated ? '+' : ''})
                </CardTitle>
                <CardBody>
                  {(artifacts?.artifacts?.length ?? 0) === 0 && <p>No artifacts yet.</p>}
                  {(artifacts?.artifacts?.length ?? 0) > 0 && (
                    <Table aria-label="Artifacts">
                      <Thead>
                        <Tr>
                          <Th>Path</Th>
                          <Th>Action</Th>
                        </Tr>
                      </Thead>
                      <Tbody>
                        {(artifacts?.artifacts ?? []).map((path) => (
                          <Tr key={path}>
                            <Td>{path}</Td>
                            <Td>
                              <Button variant="link" onClick={() => downloadArtifact(jobId, path)}>
                                Download
                              </Button>
                            </Td>
                          </Tr>
                        ))}
                      </Tbody>
                    </Table>
                  )}
                </CardBody>
              </Card>
            </GridItem>
          </Grid>
        </>
      )}
    </PageSection>
  );
}
