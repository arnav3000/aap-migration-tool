import { useState, useEffect, useCallback } from 'react';
import {
  Title,
  TextContent,
  Text,
  Button,
  Card,
  CardBody,
  Alert,
  Modal,
  ModalVariant,
  Form,
  FormGroup,
  TextInput,
  TextArea,
  FormSelect,
  FormSelectOption,
  Label,
  Split,
  SplitItem,
} from '@patternfly/react-core';
import { Table, Thead, Tbody, Tr, Th, Td } from '@patternfly/react-table';
import { useNavigate } from 'react-router-dom';
import { api } from '../api/client';
import type { Connection } from '../types/connection';
import type { MigrationPlanListItem } from '../types/resources';

export function Planner() {
  const [plans, setPlans] = useState<MigrationPlanListItem[]>([]);
  const [connections, setConnections] = useState<Connection[]>([]);
  const [showCreate, setShowCreate] = useState(false);
  const [newName, setNewName] = useState('');
  const [newDesc, setNewDesc] = useState('');
  const [newDestId, setNewDestId] = useState('');
  const [creating, setCreating] = useState(false);
  const [error, setError] = useState('');
  const navigate = useNavigate();

  const loadPlans = useCallback(async () => {
    try {
      const result = await api.listPlans() as MigrationPlanListItem[];
      setPlans(result);
    } catch { /* ignore */ }
  }, []);

  const loadConnections = useCallback(async () => {
    try {
      const conns = await api.listConnections() as Connection[];
      setConnections(conns);
    } catch { /* ignore */ }
  }, []);

  useEffect(() => { loadPlans(); loadConnections(); }, [loadPlans, loadConnections]);

  const handleCreate = async () => {
    if (!newName.trim() || !newDestId) return;
    setCreating(true);
    setError('');
    try {
      const plan = await api.createPlan({
        name: newName.trim(),
        description: newDesc.trim(),
        destination_id: newDestId,
      }) as { id: string };
      setShowCreate(false);
      setNewName('');
      setNewDesc('');
      setNewDestId('');
      navigate(`/planner/${plan.id}`);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setCreating(false);
    }
  };

  const handleDelete = async (id: string) => {
    setPlans(prev => prev.filter(p => p.id !== id));
    try {
      await api.deletePlan(id);
    } catch { /* already removed from UI */ }
    loadPlans();
  };

  const statusColor = (status: string) => {
    switch (status) {
      case 'draft': return 'blue';
      case 'active': return 'orange';
      case 'completed': return 'green';
      case 'failed': return 'red';
      default: return 'grey';
    }
  };

  const destinations = connections.filter(c => c.type === 'aap' || c.role === 'destination');

  return (
    <>
      <Split hasGutter style={{ marginBottom: 16 }}>
        <SplitItem isFilled>
          <Title headingLevel="h1" size="2xl">Migration Planner</Title>
          <TextContent>
            <Text>Create multi-source, phased migration plans based on dependency analysis.</Text>
          </TextContent>
        </SplitItem>
        <SplitItem>
          <Button variant="primary" onClick={() => setShowCreate(true)}>
            Create Plan
          </Button>
        </SplitItem>
      </Split>

      {plans.length === 0 ? (
        <Card>
          <CardBody style={{ textAlign: 'center', padding: 48 }}>
            <Title headingLevel="h4" size="lg">No migration plans</Title>
            <Text component="p" style={{ margin: '12px 0' }}>
              Create a plan to organize and execute phased migrations across multiple AAP instances.
            </Text>
            <Button variant="primary" onClick={() => setShowCreate(true)}>Create Plan</Button>
          </CardBody>
        </Card>
      ) : (
        <Card>
          <CardBody>
            <Table variant="compact">
              <Thead>
                <Tr>
                  <Th>Name</Th>
                  <Th>Status</Th>
                  <Th>Sources</Th>
                  <Th>Phases</Th>
                  <Th>Updated</Th>
                  <Th>Actions</Th>
                </Tr>
              </Thead>
              <Tbody>
                {plans.map(plan => (
                  <Tr key={plan.id}>
                    <Td>
                      <Button variant="link" onClick={() => navigate(`/planner/${plan.id}`)}>
                        {plan.name}
                      </Button>
                      {plan.description && (
                        <Text component="small" style={{ color: '#6a6e73' }}>{plan.description}</Text>
                      )}
                    </Td>
                    <Td><Label color={statusColor(plan.status)} isCompact>{plan.status}</Label></Td>
                    <Td>{plan.source_count}</Td>
                    <Td>{plan.phase_count}</Td>
                    <Td>{new Date(plan.updated_at).toLocaleDateString()}</Td>
                    <Td>
                      <Button variant="link" isDanger onClick={() => handleDelete(plan.id)}>
                        Delete
                      </Button>
                    </Td>
                  </Tr>
                ))}
              </Tbody>
            </Table>
          </CardBody>
        </Card>
      )}

      <Modal
        variant={ModalVariant.small}
        title="Create Migration Plan"
        isOpen={showCreate}
        onClose={() => { setShowCreate(false); setError(''); }}
        actions={[
          <Button key="create" variant="primary" onClick={handleCreate} isDisabled={!newName.trim() || !newDestId || creating} isLoading={creating}>
            Create
          </Button>,
          <Button key="cancel" variant="link" onClick={() => { setShowCreate(false); setError(''); }}>
            Cancel
          </Button>,
        ]}
      >
        <Form>
          {error && <Alert variant="danger" isInline title={error} style={{ marginBottom: 12 }} />}
          <FormGroup label="Plan Name" isRequired fieldId="plan-name">
            <TextInput id="plan-name" value={newName} onChange={(_e, v) => setNewName(v)} placeholder="e.g. Production Consolidation" />
          </FormGroup>
          <FormGroup label="Description" fieldId="plan-desc">
            <TextArea id="plan-desc" value={newDesc} onChange={(_e, v) => setNewDesc(v)} placeholder="Optional description..." />
          </FormGroup>
          <FormGroup label="Destination" isRequired fieldId="plan-dest">
            <FormSelect id="plan-dest" value={newDestId} onChange={(_e, v) => setNewDestId(v)}>
              <FormSelectOption value="" label="-- Select destination --" isDisabled />
              {destinations.map(c => (
                <FormSelectOption key={c.id} value={c.id} label={`${c.name} (${c.url})`} />
              ))}
            </FormSelect>
          </FormGroup>
        </Form>
      </Modal>
    </>
  );
}
