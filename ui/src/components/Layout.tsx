import { useState } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router-dom';
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
  NavGroup,
  NavItem,
  NavList,
  Page,
  PageSidebar,
  PageSidebarBody,
  TextInput,
} from '@patternfly/react-core';
import {
  BundleIcon,
  CheckCircleIcon,
  CogIcon,
  HistoryIcon,
  TachometerAltIcon,
} from '@patternfly/react-icons';
import { getApiKey, setApiKey } from '../api/client';

// AWX-style navigation: grouped sections with icons under a dark masthead.
// The masthead alone carries the dark theme (pf-v6-theme-dark scope) while
// page content stays on the light theme, like the AAP console.
const NAV_SECTIONS = [
  {
    title: 'Overview',
    items: [{ to: '/', label: 'Dashboard', Icon: TachometerAltIcon }],
  },
  {
    title: 'Migration',
    items: [
      { to: '/migrate', label: 'Migrate', Icon: BundleIcon },
      { to: '/jobs', label: 'Jobs', Icon: HistoryIcon },
      { to: '/validate', label: 'Validate', Icon: CheckCircleIcon },
    ],
  },
  {
    title: 'Administration',
    items: [{ to: '/settings', label: 'Settings', Icon: CogIcon }],
  },
];

export function Layout({ children }: { children: React.ReactNode }) {
  const [keyModalOpen, setKeyModalOpen] = useState(!getApiKey());
  const [draftKey, setDraftKey] = useState(getApiKey());
  const location = useLocation();
  const navigate = useNavigate();

  const sidebar = (
    <PageSidebar>
      <PageSidebarBody>
        <Nav aria-label="Primary">
          {NAV_SECTIONS.map((section) => (
            <NavGroup key={section.title} title={section.title}>
              <NavList>
                {section.items.map((item) => (
                  <NavItem
                    key={item.to}
                    isActive={
                      item.to === '/'
                        ? location.pathname === '/'
                        : location.pathname.startsWith(item.to)
                    }
                    onClick={() => navigate(item.to)}
                  >
                    <item.Icon style={{ marginRight: 8 }} />
                    {item.label}
                  </NavItem>
                ))}
              </NavList>
            </NavGroup>
          ))}
        </Nav>
      </PageSidebarBody>
    </PageSidebar>
  );

  const masthead = (
    <div className="pf-v6-theme-dark">
      <Masthead>
        <MastheadMain>
          <MastheadBrand onClick={() => navigate('/')}>
            <span style={{ fontWeight: 700, fontSize: 18, color: '#fff' }}>AAP Bridge</span>
          </MastheadBrand>
        </MastheadMain>
        <MastheadContent>
          <span style={{ marginLeft: 'auto' }}>
            <Button variant="link" onClick={() => setKeyModalOpen(true)} style={{ color: '#fff' }}>
              {getApiKey() ? 'API key set' : 'Set API key'}
            </Button>
          </span>
        </MastheadContent>
      </Masthead>
    </div>
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

/** AWX PageHeader pattern: optional back link, title, description. */
export function PageHeader({
  title,
  description,
  backTo,
  backLabel,
}: {
  title: string;
  description?: React.ReactNode;
  backTo?: string;
  backLabel?: string;
}) {
  return (
    <div style={{ marginBottom: 16 }}>
      {backTo && (
        <div style={{ marginBottom: 4 }}>
          <NavLink to={backTo}>← {backLabel ?? 'Back'}</NavLink>
        </div>
      )}
      <h1
        style={{
          fontSize: 'var(--pf-t--global--font--size--2xl)',
          fontWeight: 'var(--pf-t--global--font--weight--body--bold)',
          margin: 0,
        }}
      >
        {title}
      </h1>
      {description && (
        <p style={{ color: 'var(--pf-t--global--text--color--subtle)', margin: '4px 0 0' }}>
          {description}
        </p>
      )}
    </div>
  );
}
