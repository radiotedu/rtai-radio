import { useEffect, useState } from 'react';

import {
  fetchStationPublicStatus,
  postStationPublicSession,
  type PublicLanguage,
  type StationId,
  type StationPublicStatusResponse,
} from './api';
import { PublicDashboard } from './components/PublicDashboard';

function App() {
  const [language, setLanguage] = useState<PublicLanguage>('en');
  const stationId: StationId = language === 'fr' ? 'radiotedu-fr' : 'radiotedu-en';
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
    setStatus(null);
    setError(null);
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

  return (
    <PublicDashboard
      status={status}
      language={language}
      connectionError={error}
      onLanguageChange={setLanguage}
    />
  );
}

function createPublicSessionId(): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    return crypto.randomUUID();
  }
  const random = Math.random().toString(36).slice(2);
  return `session_${Date.now().toString(36)}_${random.padEnd(16, '0')}`;
}

export default App;
