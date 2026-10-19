import { useEffect, useState } from 'react';
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
  Switch,
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

const THEME_KEY = 'aap-bridge-theme';
function getInitialDarkMode() {
  return localStorage.getItem(THEME_KEY) === 'dark';
}

export function Layout({ children }: { children: React.ReactNode }) {
  const [keyModalOpen, setKeyModalOpen] = useState(!getApiKey());
  const [draftKey, setDraftKey] = useState(getApiKey());
  const [isDark, setIsDark] = useState(getInitialDarkMode);
  const location = useLocation();
  const navigate = useNavigate();

  useEffect(() => {
    document.documentElement.classList.toggle('pf-v6-theme-dark', isDark);
  }, [isDark]);

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

  const updateTheme = (checked: boolean) => {
    setIsDark(checked);
    localStorage.setItem(THEME_KEY, checked ? 'dark' : 'light');
  };

  const masthead = (
    <Masthead className="aap-bridge-masthead" style={{ backgroundColor: '#151515' }}>
      <MastheadMain className="aap-bridge-masthead__main">
        <MastheadBrand onClick={() => navigate('/')}>
          <span className="aap-bridge-brand">AAP Bridge</span>
        </MastheadBrand>
      </MastheadMain>
      <MastheadContent className="aap-bridge-masthead__content">
        <Switch
          id="aap-bridge-dark-mode"
          label={<span style={{ color: '#fff' }}>Dark mode</span>}
          isChecked={isDark}
          onChange={(_event, checked) => updateTheme(checked)}
        />
        <Button
          variant="link"
          onClick={() => setKeyModalOpen(true)}
          className="aap-bridge-api-key"
          style={{ color: '#fff' }}
        >
          {getApiKey() ? 'API key set' : 'Set API key'}
        </Button>
      </MastheadContent>
    </Masthead>
  );

  return (
    <>
      <div className="aap-bridge-shell">
        <Page masthead={masthead} sidebar={sidebar} isManagedSidebar>
          {children}
        </Page>
      </div>
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
