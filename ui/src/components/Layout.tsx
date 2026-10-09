import { useState } from 'react';
import { NavLink, useNavigate } from 'react-router-dom';
import {
  Button,
  Masthead,
  MastheadMain,
  MastheadBrand,
  MastheadContent,
  Modal,
  ModalBody,
  ModalFooter,
  ModalHeader,
  Nav,
  NavItem,
  NavList,
  Page,
  PageSidebar,
  PageSidebarBody,
  TextInput,
} from '@patternfly/react-core';
import { getApiKey, setApiKey } from '../api/client';

const NAV_ITEMS = [
  { to: '/', label: 'Dashboard' },
  { to: '/migrate', label: 'Migrate' },
  { to: '/jobs', label: 'Jobs' },
  { to: '/validate', label: 'Validate' },
  { to: '/settings', label: 'Settings' },
];

export function Layout({ children }: { children: React.ReactNode }) {
  const [keyModalOpen, setKeyModalOpen] = useState(!getApiKey());
  const [draftKey, setDraftKey] = useState(getApiKey());
  const navigate = useNavigate();

  const sidebar = (
    <PageSidebar>
      <PageSidebarBody>
        <Nav aria-label="Primary">
          <NavList>
            {NAV_ITEMS.map((item) => (
              <NavItem key={item.to}>
                <NavLink to={item.to} end={item.to === '/'}>
                  {item.label}
                </NavLink>
              </NavItem>
            ))}
          </NavList>
        </Nav>
      </PageSidebarBody>
    </PageSidebar>
  );

  const masthead = (
    <Masthead>
      <MastheadMain>
        <MastheadBrand onClick={() => navigate('/')}>
          <span style={{ fontWeight: 700, fontSize: 18 }}>AAP Bridge</span>
          <span style={{ marginLeft: 8, color: '#6a6e73' }}>Migration Console</span>
        </MastheadBrand>
      </MastheadMain>
      <MastheadContent>
        <span style={{ marginLeft: 'auto' }}>
          <Button variant="link" onClick={() => setKeyModalOpen(true)}>
            {getApiKey() ? 'API key set' : 'Set API key'}
          </Button>
        </span>
      </MastheadContent>
    </Masthead>
  );

  return (
    <>
      <Page masthead={masthead} sidebar={sidebar} isManagedSidebar>
        {children}
      </Page>
      <Modal
        isOpen={keyModalOpen}
        onClose={() => setKeyModalOpen(false)}
        variant="small"
        aria-label="API key"
      >
        <ModalHeader title="API key" />
        <ModalBody>
          <p>
            Enter the value of <code>AAP_BRIDGE_API_TOKEN</code> from the API server. It is
            sent as <code>X-API-Key</code> and stored only in this browser&apos;s
            localStorage.
          </p>
          <TextInput
            type="password"
            aria-label="API key"
            placeholder="X-API-Key"
            value={draftKey}
            onChange={(_e, value) => setDraftKey(value)}
          />
        </ModalBody>
        <ModalFooter>
          <Button
            variant="primary"
            onClick={() => {
              setApiKey(draftKey.trim());
              setKeyModalOpen(false);
            }}
          >
            Save
          </Button>
          <Button variant="link" onClick={() => setKeyModalOpen(false)}>
            Cancel
          </Button>
        </ModalFooter>
      </Modal>
    </>
  );
}
