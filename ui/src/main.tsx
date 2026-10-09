import '@patternfly/react-core/dist/styles/base.css';
// PatternFly v6 CSS layers (same design system ansible-ui is built on).
import '@patternfly/patternfly/patternfly.css';
import '@patternfly/patternfly/patternfly-addons.css';
import './styles.css';

import React, { useEffect } from 'react';
import ReactDOM from 'react-dom/client';
import { BrowserRouter, Route, Routes, useLocation, useNavigate } from 'react-router-dom';
import { Layout } from './components/Layout';
import { Dashboard } from './pages/Dashboard';
import { Migrate } from './pages/Migrate';
import { Jobs } from './pages/Jobs';
import { JobDetail } from './pages/JobDetail';
import { Validate } from './pages/Validate';
import { Settings } from './pages/Settings';
import { api, getApiKey } from './api/client';
import type { ActiveConfigOut } from './api/types';

/**
 * First-load gate: nothing works until a migration pair is configured, so a
 * fresh (or wiped) server lands on /settings instead of a dead dashboard.
 * Runs once per page load only — after that the Dashboard banner nags,
 * but users can freely browse history without being trapped.
 */
function PairGate({ children }: { children: React.ReactNode }) {
  const navigate = useNavigate();
  const location = useLocation();

  useEffect(() => {
    if (!getApiKey() || location.pathname === '/settings') {
      return;
    }
    let cancelled = false;
    api
      .get<ActiveConfigOut>('/connections/active')
      .then((a) => {
        if (!cancelled && (a.source_id === null || a.target_id === null)) {
          navigate('/settings', { replace: true });
        }
      })
      .catch(() => {
        // Unreachable API / bad key: leave the user where they are (the
        // Dashboard already explains connectivity problems).
      });
    return () => {
      cancelled = true;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return <>{children}</>;
}

function App() {
  return (
    <BrowserRouter>
      <Layout>
        <PairGate>
          <Routes>
            <Route path="/" element={<Dashboard />} />
            <Route path="/migrate" element={<Migrate />} />
            <Route path="/jobs" element={<Jobs />} />
            <Route path="/jobs/:jobId" element={<JobDetail />} />
            <Route path="/validate" element={<Validate />} />
            <Route path="/settings" element={<Settings />} />
          </Routes>
        </PairGate>
      </Layout>
    </BrowserRouter>
  );
}

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
