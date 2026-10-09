import { useCallback, useEffect, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Alert,
  Button,
  Card,
  CardBody,
  CardTitle,
  Form,
  FormGroup,
  Grid,
  GridItem,
  MenuToggle,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  PageSection,
  Select,
  SelectList,
  SelectOption,
  Spinner,
  Switch,
  TextInput,
} from '@patternfly/react-core';
import { Table, Tbody, Td, Th, Thead, Tr } from '@patternfly/react-table';
import { api } from '../api/client';
import type { ActiveConfigOut, ApiConnection, ConnectionListOut } from '../api/types';
import { PageHeader } from '../components/Layout';

interface EndpointForm {
  name: string;
  url: string;
  token: string;
  verify_ssl: boolean;
  timeout: number;
}

const emptyForm = (kind: 'source' | 'target'): EndpointForm => ({
  name: '',
  url: kind === 'source' ? 'https://' : 'https://',
  token: '',
  verify_ssl: false,
  timeout: 300,
});

/**
 * Settings: all connection management in one place.
 * - Any number of source and target endpoints can be stored (tokens
 *   encrypted at rest), so the server .env only holds AAP_BRIDGE_API_TOKEN.
 * - The active migration pair is picked here; per-job overrides live on
 *   the Migrate page.
 */
export function Settings() {
  const [connections, setConnections] = useState<ApiConnection[]>([]);
  const [active, setActive] = useState<ActiveConfigOut | null>(null);
  const [pairDraft, setPairDraft] = useState<{ source_id: string; target_id: string }>({
    source_id: '',
    target_id: '',
  });
  const [pairOpen, setPairOpen] = useState<'source' | 'target' | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [modal, setModal] = useState<{
    kind: 'source' | 'target';
    editing: ApiConnection | null;
    form: EndpointForm;
    saving: boolean;
  } | null>(null);

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
      setPairDraft({ source_id: act.source_id ?? '', target_id: act.target_id ?? '' });
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to load settings');
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
  const ofKind = (kind: 'source' | 'target') => connections.filter((c) => c.kind === kind);

  const savePair = async () => {
    setError(null);
    try {
      const updated = await api.post<ActiveConfigOut>('/connections/active', {
        source_id: pairDraft.source_id || null,
        target_id: pairDraft.target_id || null,
      });
      setActive(updated);
      setNotice('Active migration pair updated.');
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to save active pair');
    }
  };

  const openModal = (kind: 'source' | 'target', editing: ApiConnection | null) => {
    setModal({
      kind,
      editing,
      form: editing
        ? { name: editing.name, url: editing.url, token: '', verify_ssl: editing.verify_ssl, timeout: editing.timeout }
        : emptyForm(kind),
      saving: false,
    });
  };

  const saveEndpoint = async () => {
    if (!modal) {
      return;
    }
    const { kind, editing, form } = modal;
    setModal({ ...modal, saving: true });
    setError(null);
    try {
      if (editing && !form.token) {
        await api.patch<ApiConnection>(`/connections/${editing.id}`, {
          name: form.name,
          url: form.url,
          verify_ssl: form.verify_ssl,
          timeout: form.timeout,
        });
      } else if (editing) {
        await api.put<ApiConnection>(`/connections/${editing.id}`, {
          name: form.name,
          url: form.url,
          token: form.token,
          verify_ssl: form.verify_ssl,
          timeout: form.timeout,
        });
      } else {
        await api.post<ApiConnection>('/connections', {
          name: form.name,
          kind,
          url: form.url,
          token: form.token,
          verify_ssl: form.verify_ssl,
          timeout: form.timeout,
        });
      }
      setModal(null);
      setNotice(
        editing ? `Endpoint '${form.name}' updated.` : `Endpoint '${form.name}' saved. Test it below, then pick the active pair.`,
      );
      await load();
    } catch (err) {
      setModal(modal);
      setError(err instanceof Error ? err.message : 'Failed to save endpoint');
    }
  };

  const testEndpoint = async (id: string) => {
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

  const removeEndpoint = async (id: string, name: string) => {
    if (!window.confirm(`Delete endpoint '${name}'? Queued jobs pinned to it will fail at execution.`)) {
      return;
    }
    setError(null);
    try {
      await api.del(`/connections/${id}`);
      setNotice(`Endpoint '${name}' deleted.`);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Failed to delete endpoint');
    }
  };

  const pairSelect = (
    which: 'source' | 'target',
    label: string,
    options: ApiConnection[],
    value: string,
  ) => (
    <FormGroup label={label} fieldId={`pair-${which}`}>
      <Select
        id={`pair-${which}`}
        isOpen={pairOpen === which}
        selected={value ? (names[value] ?? value) : 'Select…'}
        onSelect={(_e, v) => {
          setPairDraft({ ...pairDraft, [`${which}_id`]: String(v) });
          setPairOpen(null);
        }}
        onOpenChange={(open) => setPairOpen(open ? which : null)}
        toggle={(toggleRef) => (
          <MenuToggle
            ref={toggleRef}
            onClick={() => setPairOpen(pairOpen === which ? null : which)}
            isExpanded={pairOpen === which}
            style={{ minWidth: 240 }}
          >
            {value ? (names[value] ?? value) : 'Select…'}
          </MenuToggle>
        )}
      >
        <SelectList>
          {options.map((c) => (
            <SelectOption key={c.id} value={c.id}>
              {c.name} ({c.url})
            </SelectOption>
          ))}
        </SelectList>
      </Select>
    </FormGroup>
  );

  const endpointTable = (kind: 'source' | 'target', title: string, placeholder: string) => (
    <Card isFullHeight>
      <CardTitle>{title}</CardTitle>
      <CardBody>
        <div style={{ marginBottom: 12 }}>
          <Button variant="primary" onClick={() => openModal(kind, null)}>
            Add {kind} endpoint
          </Button>
        </div>
        {ofKind(kind).length === 0 && (
          <p style={{ color: '#6a6e73' }}>None yet — add one above.</p>
        )}
        {ofKind(kind).length > 0 && (
          <Table aria-label={`${kind} endpoints`}>
            <Thead>
              <Tr>
                <Th>Name</Th>
                <Th>URL</Th>
                <Th>Actions</Th>
              </Tr>
            </Thead>
            <Tbody>
              {ofKind(kind).map((c) => {
                const isActive =
                  (kind === 'source' && active?.source_id === c.id) ||
                  (kind === 'target' && active?.target_id === c.id);
                return (
                  <Tr key={c.id}>
                    <Td>
                      {c.name}
                      {isActive && ' (active)'}
                    </Td>
                    <Td>{c.url}</Td>
                    <Td>
                      <Button variant="link" onClick={() => testEndpoint(c.id)}>
                        Test
                      </Button>
                      <Button variant="link" onClick={() => openModal(kind, c)}>
                        Edit
                      </Button>
                      <Button variant="link" isDanger onClick={() => removeEndpoint(c.id, c.name)}>
                        Delete
                      </Button>
                    </Td>
                  </Tr>
                );
              })}
            </Tbody>
          </Table>
        )}
        <p style={{ color: '#6a6e73', marginTop: 8, fontSize: 13 }}>{placeholder}</p>
      </CardBody>
    </Card>
  );

  return (
    <PageSection>
      <PageHeader
        title="Settings"
        description={
          <>
            Store any number of source and target AAP endpoints — tokens are encrypted
            at rest, so the server <code>.env</code> only needs{' '}
            <code>AAP_BRIDGE_API_TOKEN</code>. Pick the active migration pair below;
            individual jobs can override it on the <Link to="/migrate">Migrate</Link> page.
          </>
        }
      />
      {loading && <Spinner aria-label="Loading settings" />}
      {error && <Alert variant="danger" title={error} style={{ marginTop: 16 }} />}
      {notice && <Alert variant="success" title={notice} style={{ marginTop: 16 }} />}
      {!loading && (
        <>
          <Card style={{ marginTop: 16 }}>
            <CardTitle>Active migration pair</CardTitle>
            <CardBody>
              <Form>
                <Grid hasGutter>
                  <GridItem span={12} md={5}>
                    {pairSelect('source', 'Source', ofKind('source'), pairDraft.source_id)}
                  </GridItem>
                  <GridItem span={12} md={5}>
                    {pairSelect('target', 'Target', ofKind('target'), pairDraft.target_id)}
                  </GridItem>
                  <GridItem span={12} md={2} style={{ alignSelf: 'end' }}>
                    <Button
                      variant="primary"
                      onClick={savePair}
                      isDisabled={!pairDraft.source_id || !pairDraft.target_id}
                    >
                      Save pair
                    </Button>
                  </GridItem>
                </Grid>
              </Form>
            </CardBody>
          </Card>
          <Grid hasGutter style={{ marginTop: 16 }}>
            <GridItem span={12} xl={6}>
              {endpointTable(
                'source',
                'Source endpoints (AAP 2.4 / 2.5)',
                'Example URL: https://aap24.example.com/api/v2',
              )}
            </GridItem>
            <GridItem span={12} xl={6}>
              {endpointTable(
                'target',
                'Target endpoints (AAP 2.5 / 2.6)',
                'AAP 2.6 uses the Platform Gateway path: https://aap26.example.com/api/controller/v2',
              )}
            </GridItem>
          </Grid>
        </>
      )}
      <Modal
        isOpen={modal !== null}
        onClose={() => setModal(null)}
        variant="medium"
        aria-label="Endpoint"
      >
        <ModalHeader title={modal?.editing ? 'Edit endpoint' : `Add ${modal?.kind} endpoint`} />
        <ModalBody>
          {modal && (
            <Form>
              <FormGroup label="Name" isRequired fieldId="ep-name">
                <TextInput
                  id="ep-name"
                  value={modal.form.name}
                  onChange={(_e, v) => setModal({ ...modal, form: { ...modal.form, name: v } })}
                  placeholder={modal.kind === 'source' ? 'prod-source' : 'prod-target'}
                />
              </FormGroup>
              <FormGroup label="URL" isRequired fieldId="ep-url">
                <TextInput
                  id="ep-url"
                  value={modal.form.url}
                  onChange={(_e, v) => setModal({ ...modal, form: { ...modal.form, url: v } })}
                />
              </FormGroup>
              <FormGroup
                label={modal.editing ? 'Token (blank = keep stored)' : 'Token'}
                isRequired={!modal.editing}
                fieldId="ep-token"
              >
                <TextInput
                  id="ep-token"
                  type="password"
                  value={modal.form.token}
                  onChange={(_e, v) => setModal({ ...modal, form: { ...modal.form, token: v } })}
                  placeholder={modal.editing ? '•••••••• (stored)' : 'Paste AAP token (write scope)'}
                />
              </FormGroup>
              <FormGroup label="Verify SSL" fieldId="ep-ssl">
                <Switch
                  id="ep-ssl"
                  isChecked={modal.form.verify_ssl}
                  onChange={(_e, c) => setModal({ ...modal, form: { ...modal.form, verify_ssl: c } })}
                />
              </FormGroup>
              <FormGroup label="Timeout (s)" fieldId="ep-timeout">
                <TextInput
                  id="ep-timeout"
                  type="number"
                  value={String(modal.form.timeout)}
                  onChange={(_e, v) =>
                    setModal({ ...modal, form: { ...modal.form, timeout: Number(v) || 300 } })
                  }
                />
              </FormGroup>
            </Form>
          )}
        </ModalBody>
        <ModalFooter>
          <Button
            variant="primary"
            onClick={saveEndpoint}
            isDisabled={
              !modal || modal.saving || !modal.form.name || !modal.form.url ||
              (!modal.editing && !modal.form.token)
            }
          >
            Save
          </Button>
          <Button variant="link" onClick={() => setModal(null)}>
            Cancel
          </Button>
        </ModalFooter>
      </Modal>
    </PageSection>
  );
}
