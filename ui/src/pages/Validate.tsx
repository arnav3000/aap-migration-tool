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

// Validate + reports: submits background jobs and navigates to the job
// detail page where stdout and report artifacts are shown AWX-style.
export function Validate() {
  const navigate = useNavigate();
  const [resourceType, setResourceType] = useState('');
  const [live, setLive] = useState(false);
  const [busy, setBusy] = useState<null | 'validate' | 'report' | 'enhanced'>(null);
  const [error, setError] = useState<string | null>(null);

  const submit = async (
    kind: 'validate' | 'report' | 'enhanced',
    path: string,
    body: object,
  ) => {
    setBusy(kind);
    setError(null);
    try {
      const created = await api.post<JobCreated>(path, body);
      navigate(`/jobs/${created.job_id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Submit failed');
    } finally {
      setBusy(null);
    }
  };

  return (
    <PageSection>
      <Title headingLevel="h1">Validate & reports</Title>
      {error && <Alert variant="danger" title={error} style={{ marginTop: 16 }} />}
      <Card style={{ marginTop: 16 }}>
        <CardTitle>Validation</CardTitle>
        <CardBody>
          <Form>
            <FormGroup label="Resource type (blank = all)" fieldId="validate-type">
              <TextInput
                id="validate-type"
                value={resourceType}
                onChange={(_e, v) => setResourceType(v)}
                placeholder="job_templates"
              />
            </FormGroup>
            <FormGroup label="Live check against target AAP" fieldId="validate-live">
              <Switch id="validate-live" isChecked={live} onChange={(_e, c) => setLive(c)} />
            </FormGroup>
          </Form>
          <div style={{ display: 'flex', gap: 8, marginTop: 12, flexWrap: 'wrap' }}>
            <Button
              variant="primary"
              isLoading={busy === 'validate'}
              onClick={() =>
                submit('validate', '/validations/run', {
                  resource_type: resourceType.trim() || null,
                  live,
                })
              }
            >
              Run validation
            </Button>
            <Button
              variant="secondary"
              isLoading={busy === 'report'}
              onClick={() =>
                submit('report', '/reports/migration', {
                  resource_type: resourceType.trim() || null,
                  output_format: 'html',
                })
              }
            >
              Migration report (HTML)
            </Button>
            <Button
              variant="secondary"
              isLoading={busy === 'enhanced'}
              onClick={() =>
                submit('enhanced', '/reports/enhanced', {
                  resource_type: resourceType.trim() || null,
                  output_format: 'html',
                })
              }
            >
              Enhanced report
            </Button>
          </div>
        </CardBody>
      </Card>
    </PageSection>
  );
}
