import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Form,
  FormGroup,
  PageSection,
  Switch,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { api } from '../api/client';
import type { JobCreated } from '../api/types';

// Full migration workflow: export -> transform -> import with job chaining.
// Each phase submits against the previous job_id so all phases share one
// working directory (exports/, xformed/, state DB), mirroring the CLI/TUI.
export function Migrate() {
  const navigate = useNavigate();
  const [resourceTypes, setResourceTypes] = useState('');
  const [skipPrep, setSkipPrep] = useState(true);
  const [dryRun, setDryRun] = useState(false);
  const [submitting, setSubmitting] = useState<null | 'export' | 'migrate' | 'granular'>(null);
  const [error, setError] = useState<string | null>(null);
  const [chain, setChain] = useState<string[]>([]);

  const parsedTypes = resourceTypes
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);

  const submitAndGo = async (label: 'export' | 'migrate' | 'granular', path: string, body: object) => {
    setSubmitting(label);
    setError(null);
    try {
      const created = await api.post<JobCreated>(path, body);
      setChain((prev) => [...prev, `${label}: ${created.job_id}`]);
      navigate(`/jobs/${created.job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Submit failed');
    } finally {
      setSubmitting(null);
    }
  };

  return (
    <PageSection>
      <Title headingLevel="h1">Migrate</Title>
      <p>
        Submit ETL phases as background jobs. Phases chain via <code>job_id</code> so
        export → transform → import share one working directory.
      </p>
      {error && <Alert variant="danger" title={error} style={{ marginTop: 16 }} />}
      {chain.length > 0 && (
        <Alert variant="info" title="Submitted" style={{ marginTop: 16 }}>
          {chain.join(' · ')}
        </Alert>
      )}
      <Card style={{ marginTop: 16 }}>
        <CardTitle>Phase scope</CardTitle>
        <CardBody>
          <Form>
            <FormGroup
              label="Resource types (comma-separated, blank = all)"
              fieldId="resource-types"
            >
              <TextInput
                id="resource-types"
                value={resourceTypes}
                onChange={(_e, v) => setResourceTypes(v)}
                placeholder="organizations, credentials, projects"
              />
            </FormGroup>
            <FormGroup label="Skip prep (schemas already discovered)" fieldId="skip-prep">
              <Switch id="skip-prep" isChecked={skipPrep} onChange={(_e, c) => setSkipPrep(c)} />
            </FormGroup>
            <FormGroup label="Dry run (import only)" fieldId="dry-run">
              <Switch id="dry-run" isChecked={dryRun} onChange={(_e, c) => setDryRun(c)} />
            </FormGroup>
          </Form>
        </CardBody>
      </Card>
      <div style={{ display: 'flex', gap: 12, marginTop: 16, flexWrap: 'wrap' }}>
        <Button
          variant="primary"
          isLoading={submitting === 'migrate'}
          onClick={() =>
            submitAndGo('migrate', '/migrations', {
              resource_types: parsedTypes.length ? parsedTypes : null,
              skip_prep: skipPrep,
              phase: 'all',
            })
          }
        >
          Run full migration
        </Button>
        <Button
          variant="secondary"
          isLoading={submitting === 'export'}
          onClick={() =>
            submitAndGo('export', '/exports', {
              resource_types: parsedTypes.length ? parsedTypes : null,
            })
          }
        >
          Export only
        </Button>
        <Button
          variant="secondary"
          isLoading={submitting === 'granular'}
          onClick={() =>
            submitAndGo('granular', '/imports/granular', {
              steps: parsedTypes.length ? parsedTypes : null,
              dry_run: dryRun,
            })
          }
        >
          Granular import
        </Button>
      </div>
      <p style={{ color: '#6a6e73', marginTop: 12 }}>
        Tip: run Export, then Transform and Import chained to the export job from the Job
        detail page for step-by-step control (mirrors the TUI granular import menu).
      </p>
    </PageSection>
  );
}
