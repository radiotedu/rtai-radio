import type { PublicLanguage, StationPublicStatusResponse, SoundTag } from '../api';

interface PublicDashboardProps {
  status: StationPublicStatusResponse;
  language: PublicLanguage;
  connectionError?: string | null;
}

const COPY = {
  en: {
    station: 'RadioTEDU English',
    listen: 'Listen live',
    now: 'Now playing',
    current: 'Current program',
    next: 'Next program',
    listeners: 'Website listeners',
    split: 'Last 14 days',
    music: 'Music',
    talking: 'Talking',
    sound: 'Sound character',
    unavailable: 'Unavailable',
    waiting: 'Waiting for the broadcasting computer.',
    stale: 'Live data is delayed. Showing the last valid station snapshot.',
    interrupted: 'Live data connection interrupted. Showing the last valid station snapshot.',
    language: 'Français',
    player: 'RadioTEDU English live stream',
  },
  fr: {
    station: 'RadioTEDU Français',
    listen: 'Écouter en direct',
    now: 'En cours',
    current: 'Émission actuelle',
    next: 'À suivre',
    listeners: 'Auditeurs sur le site',
    split: '14 derniers jours',
    music: 'Musique',
    talking: 'Parlé',
    sound: 'Ambiance sonore',
    unavailable: 'Indisponible',
    waiting: "En attente de l'ordinateur de diffusion.",
    stale: 'Les données sont en retard. Dernier état valide affiché.',
    interrupted: 'Connexion aux données interrompue. Dernier état valide affiché.',
    language: 'English',
    player: 'Flux en direct de RadioTEDU Français',
  },
} as const;

const TAG_LABELS: Record<PublicLanguage, Record<SoundTag, string>> = {
  en: {
    warm: 'Warm',
    bright: 'Bright',
    calm: 'Calm',
    focused: 'Focused',
    energetic: 'Energetic',
  },
  fr: {
    warm: 'Chaleureux',
    bright: 'Lumineux',
    calm: 'Calme',
    focused: 'Concentré',
    energetic: 'Énergique',
  },
};

export function PublicDashboard({ status, language, connectionError }: PublicDashboardProps) {
  const copy = COPY[language];
  const snapshot = status.snapshot;
  const switchHref = language === 'en' ? '/ai/fr' : '/ai/en';
  const music = status.metrics.airtime.music_percent;
  const talking = status.metrics.airtime.talking_percent;
  const tags = snapshot?.editorial.sound_tags ?? [];

  return (
    <main className="listener-page" lang={language}>
      <header className="listener-header">
        <a className="listener-wordmark" href={language === 'en' ? '/ai/en' : '/ai/fr'} aria-label={copy.station}>
          <span className="listener-signal" aria-hidden="true" />
          RadioTEDU
        </a>
        <a className="language-switch" href={switchHref} hrefLang={language === 'en' ? 'fr' : 'en'}>
          {copy.language}
        </a>
      </header>

      {(connectionError || status.stale) && snapshot ? (
        <div className="listener-notice" role="status">
          {connectionError ? copy.interrupted : copy.stale}
        </div>
      ) : null}

      <section className="listener-hero" aria-labelledby="station-heading">
        <div>
          <p className="listener-eyebrow">{snapshot?.stream.status === 'live' ? '● LIVE' : snapshot?.operational_state ?? 'OFFLINE'}</p>
          <h1 id="station-heading">{copy.station}</h1>
          <p className="listener-now-title">{snapshot?.now_playing?.title || copy.waiting}</p>
          {snapshot?.now_playing?.artist ? <p className="listener-now-artist">{snapshot.now_playing.artist}</p> : null}
        </div>
        <div className="listener-player-card">
          <span>{copy.listen}</span>
          <audio
            controls
            preload="none"
            src={snapshot?.stream.url}
            aria-label={copy.player}
            className="listener-player"
          />
          <small>{snapshot ? `${snapshot.stream.codec} · ${snapshot.stream.bitrate_kbps} kbps` : copy.unavailable}</small>
        </div>
      </section>

      <section className="listener-grid" aria-label={copy.station}>
        <InfoCard label={copy.now}>
          <strong>{snapshot?.now_playing?.title || copy.unavailable}</strong>
          <span>{snapshot?.now_playing?.artist || '—'}</span>
        </InfoCard>
        <InfoCard label={copy.current}>
          <strong>{snapshot?.current_program?.name || copy.unavailable}</strong>
          <span>{snapshot?.current_program?.vibe || '—'}</span>
        </InfoCard>
        <InfoCard label={copy.next}>
          <strong>{snapshot?.next_program?.name || copy.unavailable}</strong>
          <span>{snapshot?.next_program?.vibe || '—'}</span>
        </InfoCard>
        <InfoCard label={copy.listeners} emphasis>
          <strong>{status.metrics.active_website_listeners}</strong>
        </InfoCard>
      </section>

      <section className="listener-insights">
        <div className="airtime-card" aria-label={copy.split}>
          <p className="listener-card-label">{copy.split}</p>
          {music === null || talking === null ? (
            <p className="metric-unavailable">{copy.unavailable}</p>
          ) : (
            <>
              <div className="airtime-numbers">
                <span><b>{music}%</b>{copy.music}</span>
                <span><b>{talking}%</b>{copy.talking}</span>
              </div>
              <div className="airtime-track" aria-hidden="true">
                <span style={{ width: `${music}%` }} />
              </div>
            </>
          )}
        </div>
        <div className="sound-card" aria-label={copy.sound}>
          <p className="listener-card-label">{copy.sound}</p>
          {tags.length ? (
            <div className="sound-tags">
              {tags.map((tag) => <span key={tag}>{TAG_LABELS[language][tag]}</span>)}
            </div>
          ) : (
            <p className="metric-unavailable">{copy.unavailable}</p>
          )}
        </div>
      </section>
    </main>
  );
}

function InfoCard({
  label,
  emphasis = false,
  children,
}: {
  label: string;
  emphasis?: boolean;
  children: React.ReactNode;
}) {
  return (
    <article className={`listener-info-card${emphasis ? ' listener-info-card--emphasis' : ''}`}>
      <p className="listener-card-label">{label}</p>
      <div className="listener-card-content">{children}</div>
    </article>
  );
}
