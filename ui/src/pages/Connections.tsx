import { useCallback, useEffect, useState } from 'react';
import {
  Alert,
  Button,
  Card,
  CardBody,
  Form,
  FormGroup,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  PageSection,
  Spinner,
  Switch,
  TextInput,
  Title,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { api } from '../api/client';
import type { ActiveConfigOut, ApiConnection, ConnectionListOut } from '../api/types';

interface ConnectionForm {
  name: string;
  kind: 'source' | 'target';
  url: string;
  token: string;
  verify_ssl: boolean;
  timeout: number;
}

const EMPTY_FORM: ConnectionForm = {
  name: '',
  kind: 'source',
  url: 'https://',
  token: '',
  verify_ssl: false,
  timeout: 300,
};

export function Connections() {
  const [connections, setConnections] = useState<ApiConnection[]>([]);
  const [active, setActive] = useState<ActiveConfigOut | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [modalOpen, setModalOpen] = useState(false);
  const [form, setForm] = useState<ConnectionForm>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const [list, act] = await Promise.all([
        api.get<ConnectionListOut>('/connections?limit=100&offset=0'),
        api.get<ActiveConfigOut>('/connections/active'),
      ]);
      setConnections(list.items ?? []);
      setActive(act);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load connections');
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const names: Record<string, string> = {};
  for (const c of connections) {
    names[c.id] = c.name;
  }

  const save = async () => {
    setSaving(true);
    setError(null);
    try {
      await api.post<ApiConnection>('/connections', {
        name: form.name,
        kind: form.kind,
        url: form.url,
        token: form.token,
        verify_ssl: form.verify_ssl,
        timeout: form.timeout,
      });
      setModalOpen(false);
      setForm(EMPTY_FORM);
      setNotice('Connection saved. Activate it below to use it for migrations.');
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save connection');
    } finally {
      setSaving(false);
    }
  };

  const activate = async (id: string, kind: 'source' | 'target') => {
    setError(null);
    try {
      const body =
        kind === 'source' ? { source_id: id } : { target_id: id };
      const updated = await api.post<ActiveConfigOut>('/connections/active', body);
      setActive(updated);
      setNotice(`Active ${kind} connection updated.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to activate connection');
    }
  };

  const remove = async (id: string, name: string) => {
    if (!window.confirm(`Delete connection '${name}'? Queued jobs pinned to it will fail at execution.`)) {
      return;
    }
    setError(null);
    try {
      await api.del(`/connections/${id}`);
      setNotice(`Connection '${name}' deleted.`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete connection');
    }
  };

  const testConnection = async (id: string) => {
    setError(null);
    setNotice(null);
    try {
      const result = await api.post<{ reachable: boolean; version?: string; url: string }>(
        `/connections/${id}/test`,
        {},
      );
      setNotice(
        result.reachable
          ? `Reachable: ${result.url} (version ${result.version ?? 'unknown'})`
          : `Not reachable: ${result.url}`,
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Connection test failed');
    }
  };

  return (
    <PageSection>
      <Title headingLevel="h1">Connections</Title>
      <p>Stored AAP endpoints. The active source/target pair is used by every migration job.</p>
      {loading && <Spinner aria-label="Loading connections" />}
      {error && <Alert variant="danger" title={error} style={{ marginTop: 16 }} />}
      {notice && <Alert variant="success" title={notice} style={{ marginTop: 16 }} />}
      {!loading && (
        <>
          <div style={{ margin: '16px 0' }}>
            <Button variant="primary" onClick={() => { setForm(EMPTY_FORM); setModalOpen(true); }}>
              Add connection
            </Button>
          </div>
          <Card>
            <CardBody>
              <p>
                Active source:{' '}
                <strong>
                  {active?.source_id ? (names[active.source_id] ?? active.source_id) : 'none'}
                </strong>{' '}
                · Active target:{' '}
                <strong>
                  {active?.target_id ? (names[active.target_id] ?? active.target_id) : 'none'}
                </strong>
              </p>
            </CardBody>
          </Card>
          <Table aria-label="Connections" style={{ marginTop: 16 }}>
            <Thead>
              <Tr>
                <Th>Name</Th>
                <Th>Kind</Th>
                <Th>URL</Th>
                <Th>SSL</Th>
                <Th>Active</Th>
                <Th>Actions</Th>
              </Tr>
            </Thead>
            <Tbody>
              {connections.map((c) => (
                <Tr key={c.id}>
                  <Td>{c.name}</Td>
                  <Td>{c.kind}</Td>
                  <Td>{c.url}</Td>
                  <Td>{c.verify_ssl ? 'verify' : 'skip'}</Td>
                  <Td>
                    {(active?.source_id === c.id || active?.target_id === c.id)
                      ? 'yes'
                      : 'no'}
                  </Td>
                  <Td>
                    <Button variant="link" onClick={() => activate(c.id, c.kind as 'source' | 'target')}>
                      Activate
                    </Button>
                    <Button variant="link" onClick={() => testConnection(c.id)}>
                      Test
                    </Button>
                    <Button variant="link" isDanger onClick={() => remove(c.id, c.name)}>
                      Delete
                    </Button>
                  </Td>
                </Tr>
              ))}
            </Tbody>
          </Table>
          {connections.length === 0 && <p>No connections stored yet.</p>}
        </>
      )}
      <Modal isOpen={modalOpen} onClose={() => setModalOpen(false)} variant="medium">
        <ModalHeader title="Add connection" />
        <ModalBody>
          <Form>
            <FormGroup label="Name" isRequired fieldId="conn-name">
              <TextInput
                id="conn-name"
                value={form.name}
                onChange={(_e, v) => setForm({ ...form, name: v })}
                placeholder="prod-source"
              />
            </FormGroup>
            <FormGroup label="Kind" fieldId="conn-kind">
              <Switch
                id="conn-kind"
                label={form.kind === 'source' ? 'Source' : 'Target'}
                isChecked={form.kind === 'target'}
                onChange={(_e, checked) => setForm({ ...form, kind: checked ? 'target' : 'source' })}
              />
            </FormGroup>
            <FormGroup label="URL" isRequired fieldId="conn-url">
              <TextInput
                id="conn-url"
                value={form.url}
                onChange={(_e, v) => setForm({ ...form, url: v })}
                placeholder="https://aap24.example.com/api/v2"
              />
            </FormGroup>
            <FormGroup label="Token" isRequired fieldId="conn-token">
              <TextInput
                id="conn-token"
                type="password"
                value={form.token}
                onChange={(_e, v) => setForm({ ...form, token: v })}
              />
            </FormGroup>
            <FormGroup label="Verify SSL" fieldId="conn-ssl">
              <Switch
                id="conn-ssl"
                isChecked={form.verify_ssl}
                onChange={(_e, checked) => setForm({ ...form, verify_ssl: checked })}
              />
            </FormGroup>
            <FormGroup label="Timeout (s)" fieldId="conn-timeout">
              <TextInput
                id="conn-timeout"
                type="number"
                value={String(form.timeout)}
                onChange={(_e, v) => setForm({ ...form, timeout: Number(v) || 300 })}
              />
            </FormGroup>
          </Form>
        </ModalBody>
        <ModalFooter>
          <Button variant="primary" onClick={save} isDisabled={saving || !form.name || !form.token}>
            Save
          </Button>
          <Button variant="link" onClick={() => setModalOpen(false)}>
            Cancel
          </Button>
        </ModalFooter>
      </Modal>
    </PageSection>
  );
}
