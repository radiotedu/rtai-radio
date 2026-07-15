import { useEffect, useState } from 'react';

import { Dashboard } from './components/Dashboard';
import { PublicDashboard } from './components/PublicDashboard';
import {
  fetchStationPublicStatus,
  fetchStatus,
  postStationPublicSession,
  type PublicLanguage,
  type StationId,
  type StationPublicStatusResponse,
  type StatusResponse,
} from './api';

function App() {
  const publicRoute = /^\/ai(?:\/(en|fr))?\/?$/.exec(window.location.pathname);
  if (publicRoute) {
    const language: PublicLanguage = publicRoute[1] === 'fr' ? 'fr' : 'en';
    const stationId: StationId = language === 'fr' ? 'radiotedu-fr' : 'radiotedu-en';
    return <PublicApp language={language} stationId={stationId} />;
  }
  return <OperatorApp />;
}

function OperatorApp() {
  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const payload = await fetchStatus();
      setStatus(payload);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Backend unavailable');
    }
  }

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, []);

  if (error && !status) {
    return (
      <main className="app-shell">
        <section className="station-card station-card--narrow">
          <div className="station-title-block">
            <h1>RadioTEDU</h1>
            <p>Backend unavailable</p>
          </div>
          <div className="empty-panel">{error}</div>
        </section>
      </main>
    );
  }

  if (!status) {
    return (
      <main className="app-shell">
        <section className="station-card station-card--narrow">
          <div className="station-title-block">
            <h1>RadioTEDU</h1>
            <p>Loading local station state</p>
          </div>
        </section>
      </main>
    );
  }

  return <Dashboard status={status} onRefresh={() => void refresh()} />;
}

function PublicApp({ language, stationId }: { language: PublicLanguage; stationId: StationId }) {
  const [status, setStatus] = useState<StationPublicStatusResponse | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function refresh() {
    try {
      const payload = await fetchStationPublicStatus(stationId);
      setStatus(payload);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : 'Listener page unavailable');
    }
  }

  useEffect(() => {
    void refresh();
    const timer = window.setInterval(() => void refresh(), 5000);
    return () => window.clearInterval(timer);
  }, [stationId]);

  useEffect(() => {
    const sessionId = createPublicSessionId();
    let ended = false;
    void postStationPublicSession(stationId, 'start', sessionId);
    const heartbeat = window.setInterval(
      () => void postStationPublicSession(stationId, 'heartbeat', sessionId),
      20_000,
    );
    const endSession = () => {
      if (ended) return;
      ended = true;
      void postStationPublicSession(stationId, 'end', sessionId, true);
    };
    window.addEventListener('pagehide', endSession);
    return () => {
      window.clearInterval(heartbeat);
      window.removeEventListener('pagehide', endSession);
      endSession();
    };
  }, [stationId]);

  if (error && !status) {
    return (
      <main className="app-shell">
        <section className="station-card station-card--narrow">
          <div className="station-title-block">
            <h1>RadioTEDU</h1>
            <p>{language === 'fr' ? 'Page auditeur indisponible' : 'Listener page unavailable'}</p>
          </div>
          <div className="empty-panel">{error}</div>
        </section>
      </main>
    );
  }

  if (!status) {
    return (
      <main className="app-shell">
        <section className="station-card station-card--narrow">
          <div className="station-title-block">
            <h1>RadioTEDU</h1>
            <p>{language === 'fr' ? 'Chargement de la station' : 'Loading station'}</p>
          </div>
        </section>
      </main>
    );
  }

  return <PublicDashboard status={status} language={language} connectionError={error} />;
}

function createPublicSessionId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  const random = Math.random().toString(36).slice(2);
  return `session_${Date.now().toString(36)}_${random.padEnd(16, '0')}`;
}

export default App;
