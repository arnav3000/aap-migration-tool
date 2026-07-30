import { useState } from 'react';
import {
  Modal,
  ModalVariant,
  Form,
  FormGroup,
  FormHelperText,
  HelperText,
  HelperTextItem,
  TextInput,
  FormSelect,
  FormSelectOption,
  Checkbox,
  Button,
  Alert,
} from '@patternfly/react-core';
import type { Connection } from '../types/connection';

interface Props {
  isOpen: boolean;
  initial?: Partial<Connection>;
  onSave: (conn: Omit<Connection, 'id'>) => void;
  onClose: () => void;
  error?: string | null;
}

export function ConnectionForm({ isOpen, initial, onSave, onClose, error }: Props) {
  const isEdit = !!initial?.name;
  const [name, setName] = useState(initial?.name || '');
  const [type, setType] = useState<'awx' | 'aap'>(initial?.type || 'awx');
  const [role, setRole] = useState<'source' | 'destination'>(initial?.role || 'source');
  const [url, setUrl] = useState(initial?.url || '');
  const [token, setToken] = useState('');
  const [verifySsl, setVerifySsl] = useState(initial?.verify_ssl ?? true);

  const handleSubmit = () => {
    const conn: Record<string, unknown> = { name, type, role, url, verify_ssl: verifySsl };
    if (token) {
      conn.token = token;
    } else if (!isEdit) {
      conn.token = null;
    }
    onSave(conn as Omit<Connection, 'id'>);
  };

  return (
    <Modal
      isOpen={isOpen}
      onClose={onClose}
      variant={ModalVariant.medium}
      title={isEdit ? 'Edit Connection' : 'Add Connection'}
      actions={[
        <Button key="save" variant="primary" onClick={handleSubmit}>Save</Button>,
        <Button key="cancel" variant="link" onClick={onClose}>Cancel</Button>,
      ]}
    >
      <Form isHorizontal>
        {error && (
          <Alert variant="danger" isInline title="Save failed" style={{ marginBottom: 16 }}>
            {error}
          </Alert>
        )}
        <FormGroup label="Name" isRequired fieldId="name">
          <TextInput id="name" value={name} onChange={(_e, v) => setName(v)} placeholder="My AAP Instance" />
        </FormGroup>
        <FormGroup label="Type" fieldId="type">
          <FormSelect id="type" value={type} onChange={(_e, v) => {
            const t = v as 'awx' | 'aap';
            setType(t);
            if (t === 'aap') {
              setRole('destination');
            } else {
              setRole('source');
            }
          }}>
            <FormSelectOption value="awx" label="AWX" />
            <FormSelectOption value="aap" label="AAP" />
          </FormSelect>
        </FormGroup>
        <FormGroup label="Role" fieldId="role">
          <FormSelect id="role" value={role} onChange={(_e, v) => setRole(v as 'source' | 'destination')}
            isDisabled={type === 'awx'}
          >
            <FormSelectOption value="source" label="Source (migrate FROM)" />
            <FormSelectOption value="destination" label="Destination (migrate TO)" />
          </FormSelect>
          <FormHelperText>
            <HelperText>
              <HelperTextItem>
                {type === 'awx' ? 'AWX instances are always sources' : 'AAP can be source (older) or destination (2.5+)'}
              </HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>
        <FormGroup label="URL" isRequired fieldId="url">
          <TextInput id="url" value={url} onChange={(_e, v) => setUrl(v)} placeholder="https://aap.example.com" />
          <FormHelperText>
            <HelperText>
              <HelperTextItem>Full URL including protocol (e.g., https://aap.example.com)</HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>
        <FormGroup label="Token" fieldId="token">
          <TextInput id="token" type="password" value={token} onChange={(_e, v) => setToken(v)} placeholder={isEdit ? 'Leave blank to keep current token' : 'API authentication token'} />
          <FormHelperText>
            <HelperText>
              <HelperTextItem>Personal Access Token or OAuth2 token for API authentication</HelperTextItem>
            </HelperText>
          </FormHelperText>
        </FormGroup>
        <FormGroup fieldId="verify-ssl">
          <Checkbox
            id="verify-ssl"
            label="Verify SSL certificate"
            isChecked={verifySsl}
            onChange={(_e, v) => setVerifySsl(v)}
          />
        </FormGroup>
      </Form>
    </Modal>
  );
}
