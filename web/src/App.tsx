import { useState, useEffect, Component, type ReactNode } from 'react';
import { BrowserRouter, Routes, Route, NavLink, useLocation, Link } from 'react-router-dom';
import {
  Page,
  Masthead,
  MastheadMain,
  MastheadBrand,
  MastheadContent,
  MastheadToggle,
  PageToggleButton,
  Nav,
  NavItem,
  NavList,
  NavExpandable,
  PageSidebar,
  PageSidebarBody,
  PageSection,
  Toolbar,
  ToolbarContent,
  ToolbarGroup,
  ToolbarItem,
  Alert,
  Button,
} from '@patternfly/react-core';
import { Dropdown, DropdownItem, KebabToggle } from '@patternfly/react-core/deprecated';
import BarsIcon from '@patternfly/react-icons/dist/esm/icons/bars-icon';
import QuestionCircleIcon from '@patternfly/react-icons/dist/esm/icons/question-circle-icon';
import SunIcon from '@patternfly/react-icons/dist/esm/icons/sun-icon';
import MoonIcon from '@patternfly/react-icons/dist/esm/icons/moon-icon';

class AppErrorBoundary extends Component<{ children: ReactNode }, { error: Error | null }> {
  state = { error: null as Error | null };
  static getDerivedStateFromError(error: Error) { return { error }; }
  render() {
    if (this.state.error) {
      return (
        <div style={{ padding: 48 }}>
          <Alert variant="danger" isInline title="Application Error">
            <p>Something went wrong. Please try refreshing the page.</p>
            <pre style={{ fontSize: '0.8em', whiteSpace: 'pre-wrap', marginTop: 12, maxHeight: 300, overflow: 'auto' }}>
              {this.state.error.message}
              {'\n\n'}
              {this.state.error.stack}
            </pre>
            <Button variant="primary" onClick={() => { this.setState({ error: null }); window.location.reload(); }} style={{ marginTop: 12 }}>
              Reload Page
            </Button>
          </Alert>
        </div>
      );
    }
    return this.props.children;
  }
}

function useTheme() {
  const [dark, setDark] = useState(() => {
    const stored = localStorage.getItem('theme');
    if (stored) return stored === 'dark';
    return window.matchMedia('(prefers-color-scheme: dark)').matches;
  });

  const toggle = () => {
    const next = !dark;
    setDark(next);
    localStorage.setItem('theme', next ? 'dark' : 'light');
    document.documentElement.classList.toggle('pf-v5-theme-dark', next);
  };

  useEffect(() => {
    document.documentElement.classList.toggle('pf-v5-theme-dark', dark);
  }, [dark]);

  return { dark, toggle };
}
import { Dashboard } from './pages/Dashboard';
import { Operations } from './pages/Operations';
import { Migrate } from './pages/Migrate';
import { ObjectBrowser } from './pages/ObjectBrowser';
import { Jobs } from './pages/Jobs';
import { JobDetail } from './pages/JobDetail';
import { Analysis } from './pages/Analysis';
import { Sizing } from './pages/Sizing';
import { Planner } from './pages/Planner';
import { PlanDetail } from './pages/PlanDetail';

function AppNav() {
  const location = useLocation();
  const path = location.pathname;

  const isActive = (route: string) => path.startsWith(route);

  const [migrationOpen, setMigrationOpen] = useState(
    () => JSON.parse(localStorage.getItem('nav-migration-open') ?? 'true')
  );
  const [planningOpen, setPlanningOpen] = useState(
    () => JSON.parse(localStorage.getItem('nav-planning-open') ?? 'true')
  );

  const toggleMigration = () => {
    const next = !migrationOpen;
    setMigrationOpen(next);
    localStorage.setItem('nav-migration-open', JSON.stringify(next));
  };

  const togglePlanning = () => {
    const next = !planningOpen;
    setPlanningOpen(next);
    localStorage.setItem('nav-planning-open', JSON.stringify(next));
  };

  return (
    <Nav>
      <NavList>
        <NavItem isActive={isActive('/jobs')}>
          <NavLink to="/jobs">Jobs</NavLink>
        </NavItem>

        <NavExpandable
          title="Planning"
          isExpanded={planningOpen}
          onExpand={togglePlanning}
          isActive={isActive('/analysis') || isActive('/sizing') || isActive('/browse')}
        >
          <NavItem isActive={isActive('/analysis')}>
            <NavLink to="/analysis">Dependency Analysis</NavLink>
          </NavItem>
          <NavItem isActive={isActive('/browse')}>
            <NavLink to="/browse">Object Browser</NavLink>
          </NavItem>
          <NavItem isActive={isActive('/sizing')}>
            <NavLink to="/sizing">Sizing Calculator</NavLink>
          </NavItem>
        </NavExpandable>

        <NavExpandable
          title="Migration"
          isExpanded={migrationOpen}
          onExpand={toggleMigration}
          isActive={isActive('/migrate') || isActive('/operations') || isActive('/planner')}
        >
          <NavItem isActive={isActive('/planner')}>
            <NavLink to="/planner">Migration Planner</NavLink>
          </NavItem>
          <NavItem isActive={isActive('/migrate')}>
            <NavLink to="/migrate">Migrate</NavLink>
          </NavItem>
          <NavItem isActive={isActive('/operations')}>
            <NavLink to="/operations">Operations</NavLink>
          </NavItem>
        </NavExpandable>

        <NavItem isActive={isActive('/settings')}>
          <NavLink to="/settings">Settings</NavLink>
        </NavItem>
      </NavList>
    </Nav>
  );
}

export function App() {
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [helpOpen, setHelpOpen] = useState(false);
  const { dark, toggle: toggleTheme } = useTheme();

  const header = (
    <Masthead display={{ default: 'inline' }}>
      <MastheadToggle>
        <PageToggleButton
          variant="plain"
          aria-label="Global navigation"
          isSidebarOpen={sidebarOpen}
          onSidebarToggle={() => setSidebarOpen(prev => !prev)}
        >
          <BarsIcon />
        </PageToggleButton>
      </MastheadToggle>
      <MastheadMain>
        <MastheadBrand>
          <Link to="/" style={{ textDecoration: 'none', color: 'white', fontSize: '1.25rem', fontWeight: 600 }}>
            AAP Bridge
          </Link>
        </MastheadBrand>
      </MastheadMain>
      <MastheadContent>
        <Toolbar inset={{ default: 'insetNone' }}>
          <ToolbarContent>
            <ToolbarGroup align={{ default: 'alignRight' }}>
              <ToolbarItem>
                <PageToggleButton
                  variant="plain"
                  aria-label="Toggle dark mode"
                  onClick={toggleTheme}
                  style={{ color: 'white' }}
                >
                  {dark ? <SunIcon /> : <MoonIcon />}
                </PageToggleButton>
              </ToolbarItem>
              <ToolbarItem>
                <Dropdown
                  isOpen={helpOpen}
                  onSelect={() => setHelpOpen(false)}
                  toggle={
                    <KebabToggle onToggle={(_e, open) => setHelpOpen(open)}>
                      <QuestionCircleIcon style={{ color: 'white' }} />
                    </KebabToggle>
                  }
                  isPlain
                  position="right"
                  dropdownItems={[
                    <DropdownItem key="docs" component="a" href="https://github.com/arnav3000/aap-migration-tool" target="_blank">
                      Documentation
                    </DropdownItem>,
                    <DropdownItem key="repo" component="a" href="https://github.com/arnav3000/aap-migration-tool" target="_blank">
                      Source Code
                    </DropdownItem>,
                  ]}
                />
              </ToolbarItem>
            </ToolbarGroup>
          </ToolbarContent>
        </Toolbar>
      </MastheadContent>
    </Masthead>
  );

  const sidebar = (
    <PageSidebar isSidebarOpen={sidebarOpen}>
      <PageSidebarBody>
        <AppNav />
      </PageSidebarBody>
    </PageSidebar>
  );

  return (
    <BrowserRouter>
      <AppErrorBoundary>
        <Page header={header} sidebar={sidebar} isManagedSidebar={false}>
          <PageSection>
            <Routes>
              <Route path="/" element={<Jobs />} />
              <Route path="/jobs" element={<Jobs />} />
              <Route path="/jobs/:id" element={<JobDetail />} />
              <Route path="/analysis" element={<Analysis />} />
              <Route path="/browse" element={<ObjectBrowser />} />
              <Route path="/sizing" element={<Sizing />} />
              <Route path="/planner" element={<Planner />} />
              <Route path="/planner/:id" element={<PlanDetail />} />
              <Route path="/migrate" element={<Migrate />} />
              <Route path="/operations" element={<Operations />} />
              <Route path="/settings" element={<Dashboard />} />
            </Routes>
          </PageSection>
        </Page>
      </AppErrorBoundary>
    </BrowserRouter>
  );
}
