import { useEffect, useMemo, useRef, useState } from 'react';
import { Pause, Play } from 'lucide-react';

import type { PublicLanguage, StationPublicStatusResponse, SoundTag } from '../api';

interface PublicDashboardProps {
  status: StationPublicStatusResponse;
  language: PublicLanguage;
  connectionError?: string | null;
  onLanguageChange?: (language: PublicLanguage) => void;
}

type ProgramKey =
  | 'overnight_signal'
  | 'morning_signal'
  | 'campus_frequencies'
  | 'night_lab'
  | 'weekend_overnight'
  | 'weekend_transmission';

interface ProgramCard {
  id: ProgramKey;
  en: { name: string; description: string; days: string };
  fr: { name: string; description: string; days: string };
  time: string;
}

const PROGRAMS: ProgramCard[] = [
  {
    id: 'overnight_signal',
    en: { name: 'Night Signal', description: 'Warm, focused pop for the quiet hours before campus wakes.', days: 'Mon–Fri' },
    fr: { name: 'Signal de nuit', description: 'Une pop chaleureuse et concentrée avant le réveil du campus.', days: 'Lun–Ven' },
    time: '00:00–05:59',
  },
  {
    id: 'morning_signal',
    en: { name: 'TEDU Dawn', description: 'Bright, energetic music and a concise AI-led morning handoff.', days: 'Mon–Fri' },
    fr: { name: 'Aube TEDU', description: 'Musique lumineuse et lancement matinal concis, mené par l’IA.', days: 'Lun–Ven' },
    time: '06:00–09:59',
  },
  {
    id: 'campus_frequencies',
    en: { name: 'Campus Flow', description: 'A steady daytime mix built for movement, study and campus life.', days: 'Mon–Fri' },
    fr: { name: 'Flux du campus', description: 'Un mix de journée pour bouger, étudier et vivre le campus.', days: 'Lun–Ven' },
    time: '10:00–17:59',
  },
  {
    id: 'night_lab',
    en: { name: 'Jazz Lab', description: 'Late-evening pop and jazz with room for AI-hosted live segments.', days: 'Every day' },
    fr: { name: 'Laboratoire jazz', description: 'Pop et jazz du soir, avec des séquences en direct animées par l’IA.', days: 'Tous les jours' },
    time: '18:00–23:59',
  },
  {
    id: 'weekend_overnight',
    en: { name: 'Weekend Night Signal', description: 'A slower, spacious overnight selection for Saturday and Sunday.', days: 'Sat–Sun' },
    fr: { name: 'Signal de nuit du week-end', description: 'Une sélection nocturne plus lente et aérée pour le week-end.', days: 'Sam–Dim' },
    time: '00:00–07:59',
  },
  {
    id: 'weekend_transmission',
    en: { name: 'Weekend Signal', description: 'Sunny, eclectic daytime radio with a relaxed weekend pace.', days: 'Sat–Sun' },
    fr: { name: 'Signal du week-end', description: 'Une radio éclectique et ensoleillée au rythme détendu du week-end.', days: 'Sam–Dim' },
    time: '08:00–17:59',
  },
];

const COPY = {
  en: {
    kicker: 'A continuous university-radio experiment from Ankara',
    title: 'Can AI lead a real radio station?',
    intro: 'RadioTEDU gives RTAI a live booth, a local music library and a weekly schedule. It selects the next track, prepares short links and keeps two language channels moving around the clock.',
    capabilities: 'Agent capabilities',
    capabilityItems: ['Play local music', 'Schedule programs', 'Host live segments', 'Watch the signal', 'Read listener stats'],
    station: 'RadioTEDU English',
    stationLine: 'AI-led English channel',
    listen: 'Listen live',
    pause: 'Pause live radio',
    now: 'Now playing',
    listeners: 'Current listeners',
    aiHost: 'RTAI host',
    onMic: 'On microphone',
    curating: 'Curating in the background',
    schedule: 'Schedule',
    scheduleIntro: 'Six original programs follow the real Ankara clock. The artwork changes with the daypart.',
    current: 'On now',
    next: 'Up next',
    last14: 'Last 14 days',
    music: 'Music',
    talking: 'Talking',
    sound: 'Sound character',
    unavailable: 'Unavailable',
    waiting: 'Waiting for the broadcast signal',
    live: 'Live',
    offline: 'Offline',
    delayed: 'Delayed',
    noticeStale: 'Live data is delayed. Showing the last valid station snapshot.',
    noticeError: 'Live data connection interrupted. Showing the last valid station snapshot.',
    player: 'RadioTEDU English live stream',
    sectionTitle: 'No listener steering. No store.',
    sectionBody: 'RTAI leads the programming inside fixed broadcast and safety rules. Listeners hear the result and see honest station status while all programming actions stay inside the broadcast system.',
    foreverTitle: 'A broadcast that keeps running',
    foreverBody: 'The AI prepares links while music is playing, follows the published schedule and hands the live stream back to music if speech is unavailable.',
  },
  fr: {
    kicker: 'Une expérience continue de radio universitaire depuis Ankara',
    title: 'Une IA peut-elle diriger une vraie radio ?',
    intro: 'RadioTEDU confie à RTAI un studio en direct, une discothèque locale et une grille hebdomadaire. Elle choisit le morceau suivant, prépare de courtes interventions et anime deux chaînes linguistiques en continu.',
    capabilities: 'Capacités de l’agent',
    capabilityItems: ['Diffuser la musique locale', 'Planifier les émissions', 'Animer en direct', 'Surveiller le signal', 'Lire les statistiques'],
    station: 'RadioTEDU Français',
    stationLine: 'Chaîne française menée par l’IA',
    listen: 'Écouter en direct',
    pause: 'Mettre la radio en pause',
    now: 'En cours',
    listeners: 'Auditeurs actuels',
    aiHost: 'Animateur RTAI',
    onMic: 'Au micro',
    curating: 'Programmation en arrière-plan',
    schedule: 'Grille',
    scheduleIntro: 'Six émissions originales suivent l’heure réelle d’Ankara. L’image change selon le moment de la journée.',
    current: 'Émission actuelle',
    next: 'À suivre',
    last14: '14 derniers jours',
    music: 'Musique',
    talking: 'Parlé',
    sound: 'Caractère sonore',
    unavailable: 'Indisponible',
    waiting: 'En attente du signal de diffusion',
    live: 'En direct',
    offline: 'Hors ligne',
    delayed: 'En retard',
    noticeStale: 'Les données sont en retard. Le dernier état valide reste affiché.',
    noticeError: 'La connexion aux données a été interrompue. Le dernier état valide reste affiché.',
    player: 'Flux en direct de RadioTEDU Français',
    sectionTitle: 'Pas de pilotage auditeur. Pas de boutique.',
    sectionBody: 'RTAI dirige la programmation dans des règles fixes de diffusion et de sécurité. Le public écoute et consulte un état honnête tandis que les actions de programmation restent internes au système de diffusion.',
    foreverTitle: 'Une diffusion qui continue',
    foreverBody: 'L’IA prépare ses interventions pendant la musique, suit la grille publiée et rend l’antenne à la musique si la parole n’est pas disponible.',
  },
} as const;

const TAG_LABELS: Record<PublicLanguage, Record<SoundTag, string>> = {
  en: { warm: 'Warm', bright: 'Bright', calm: 'Calm', focused: 'Focused', energetic: 'Energetic' },
  fr: { warm: 'Chaleureux', bright: 'Lumineux', calm: 'Calme', focused: 'Concentré', energetic: 'Énergique' },
};

const STREAMS: Record<PublicLanguage, string> = {
  en: 'https://stream.radiotedu.com/ai',
  fr: 'https://stream.radiotedu.com/event',
};

function scheduledProgramNow(): ProgramKey {
  const parts = new Intl.DateTimeFormat('en-GB', {
    timeZone: 'Europe/Istanbul',
    weekday: 'short',
    hour: '2-digit',
    hour12: false,
  }).formatToParts(new Date());
  const weekday = parts.find((part) => part.type === 'weekday')?.value ?? 'Mon';
  const hour = Number(parts.find((part) => part.type === 'hour')?.value ?? 12) % 24;
  const weekend = weekday === 'Sat' || weekday === 'Sun';
  if (weekend) return hour < 8 ? 'weekend_overnight' : hour < 18 ? 'weekend_transmission' : 'night_lab';
  return hour < 6 ? 'overnight_signal' : hour < 10 ? 'morning_signal' : hour < 18 ? 'campus_frequencies' : 'night_lab';
}

function localizedProgram(id: string | undefined, language: PublicLanguage) {
  const aliases: Record<string, ProgramKey> = {
    'jazz-lab': 'night_lab',
    'tedu-dawn': 'morning_signal',
    'campus-flow': 'campus_frequencies',
    'night-signal': 'overnight_signal',
    'weekend-night-signal': 'weekend_overnight',
    'weekend-signal': 'weekend_transmission',
  };
  const normalizedId = id ? aliases[id] ?? id : undefined;
  const fallback = PROGRAMS.find((program) => program.id === scheduledProgramNow())!;
  const program = PROGRAMS.find((candidate) => candidate.id === normalizedId) ?? fallback;
  return { ...program, ...program[language] };
}

export function PublicDashboard({ status, language, connectionError, onLanguageChange = () => undefined }: PublicDashboardProps) {
  const copy = COPY[language];
  const snapshot = status.snapshot;
  const music = status.metrics.airtime.music_percent;
  const talking = status.metrics.airtime.talking_percent;
  const tags = snapshot?.editorial.sound_tags ?? [];
  const audioRef = useRef<HTMLAudioElement>(null);
  const [playing, setPlaying] = useState(false);
  // Stream routing is a website release decision. Never let a delayed status
  // snapshot silently switch the listener to a different Icecast mount.
  const streamUrl = STREAMS[language];
  const live = Boolean(status.online && snapshot?.stream.status === 'live');
  const delayed = Boolean(connectionError || status.stale);
  const currentProgram = useMemo(() => localizedProgram(snapshot?.current_program?.id, language), [snapshot?.current_program?.id, language]);
  const nextProgram = useMemo(() => localizedProgram(snapshot?.next_program?.id, language), [snapshot?.next_program?.id, language]);

  useEffect(() => {
    const audio = audioRef.current;
    if (!audio) return;
    audio.pause();
    audio.load();
    setPlaying(false);
  }, [streamUrl]);

  async function togglePlayback() {
    const audio = audioRef.current;
    if (!audio || !live) return;
    if (audio.paused) {
      try {
        await audio.play();
      } catch {
        setPlaying(false);
      }
    } else {
      audio.pause();
    }
  }

  return (
    <main className="rtfm" lang={language}>
      <header className="rtfm__nav">
        <a href="/ai" className="rtfm__brand" aria-label="RadioTEDU"><img src="/brand/radiotedu-logo-white.png" alt="RadioTEDU" /></a>
        <div className="rtfm__language" role="group" aria-label="Station language">
          <button type="button" className={language === 'en' ? 'is-active' : ''} aria-pressed={language === 'en'} onClick={() => onLanguageChange('en')}>EN</button>
          <button type="button" className={language === 'fr' ? 'is-active' : ''} aria-pressed={language === 'fr'} onClick={() => onLanguageChange('fr')}>FR</button>
        </div>
        <div className="rtfm__rtai"><span>Led by</span><img src="/brand/rtai-logo.png" alt="RTAI" /></div>
      </header>

      <section className="rtfm__intro">
        <div className="rtfm__intro-copy">
          <p className="rtfm__kicker">{copy.kicker}</p>
          <h1>RadioTEDU <em>AI</em></h1>
          <h2>{copy.title}</h2>
          <p className="rtfm__lede">{copy.intro}</p>
        </div>

        <aside className="rtfm__hero-listener" aria-label={`${copy.station} listener`}>
          <div className="rtfm__hero-status">
            <span className={`rtfm__live${live ? ' is-live' : ''}`}><i />{delayed ? copy.delayed : live ? copy.live : copy.offline}</span>
            <strong>{copy.station}</strong>
          </div>
          <div className="rtfm__hero-now">
            <p>{copy.now}</p>
            <h3>{snapshot?.now_playing?.title || copy.waiting}</h3>
            <span>{snapshot?.now_playing?.artist || currentProgram.name}</span>
          </div>
          <div className="rtfm__hero-controls">
            <button type="button" className="rtfm__play" onClick={() => void togglePlayback()} disabled={!live} aria-label={playing ? copy.pause : copy.listen}>
              {playing ? <Pause aria-hidden="true" /> : <Play aria-hidden="true" />}
            </button>
            <div><strong>{playing ? copy.live : copy.listen}</strong><span>MP3 · 192 kbps · {language.toUpperCase()}</span></div>
          </div>
          <dl className="rtfm__hero-facts">
            <div><dt>{copy.listeners}</dt><dd>{status.metrics.active_website_listeners}</dd></div>
            <div><dt>{copy.current}</dt><dd>{currentProgram.name}<small>{currentProgram.time}</small></dd></div>
          </dl>
          <audio ref={audioRef} preload="none" src={streamUrl} aria-label={copy.player} onPlaying={() => setPlaying(true)} onPause={() => setPlaying(false)} />
        </aside>
      </section>

      <section className="rtfm__capabilities" aria-labelledby="capability-title">
        <h2 id="capability-title">{copy.capabilities}</h2>
        <div>{copy.capabilityItems.map((item, index) => <span key={item}><b>{String(index + 1).padStart(2, '0')}</b>{item}</span>)}</div>
      </section>

      {(connectionError || status.stale) && snapshot ? <div className="rtfm__notice" role="status">{connectionError ? copy.noticeError : copy.noticeStale}</div> : null}

      <section className="rtfm__station" aria-labelledby="station-title">
        <div className="rtfm__cover">
          <img src={`/programs/${currentProgram.id}.png`} alt={`${currentProgram.name} program cover`} />
          <div className="rtfm__cover-brand"><img src="/brand/radiotedu-logo-white.png" alt="" /><span>{currentProgram.time}</span></div>
          <div className="rtfm__cover-title"><span>{currentProgram.days}</span><strong>{currentProgram.name}</strong></div>
        </div>

        <div className="rtfm__station-body">
          <div className="rtfm__station-heading">
            <div><p>{copy.stationLine}</p><h2 id="station-title">{copy.station}</h2></div>
            <span className={`rtfm__live${live ? ' is-live' : ''}`}><i />{delayed ? copy.delayed : live ? copy.live : copy.offline}</span>
          </div>

          <div className="rtfm__now">
            <p>{copy.now}</p>
            <h3>{snapshot?.now_playing?.title || copy.waiting}</h3>
            <span>{snapshot?.now_playing?.artist || currentProgram.name}</span>
          </div>

          <dl className="rtfm__facts">
            <div><dt>{copy.current}</dt><dd>{currentProgram.name}<small>{currentProgram.time}</small></dd></div>
            <div><dt>{copy.next}</dt><dd>{snapshot?.next_program ? nextProgram.name : copy.unavailable}<small>{snapshot?.next_program ? nextProgram.time : ''}</small></dd></div>
            <div><dt>{copy.aiHost}</dt><dd>{snapshot?.speech_state.active ? copy.onMic : copy.curating}</dd></div>
          </dl>

          <div className="rtfm__breakdown" aria-label={copy.last14}>
            <p>{copy.last14}</p>
            {music === null || talking === null ? <span>{copy.unavailable}</span> : <>
              <div className="rtfm__bar"><i style={{ width: `${music}%` }} /></div>
              <div className="rtfm__bar-labels"><span>{copy.music} <b>{music}%</b></span><span>{copy.talking} <b>{talking}%</b></span></div>
            </>}
          </div>

          <div className="rtfm__tags"><p>{copy.sound}</p><div>{tags.length ? tags.map((tag) => <span key={tag}>{TAG_LABELS[language][tag]}</span>) : <span>{copy.unavailable}</span>}</div></div>
        </div>
      </section>

      <section className="rtfm__schedule" aria-labelledby="schedule-title">
        <div className="rtfm__section-head"><p>24 / 7 · Europe/Istanbul</p><h2 id="schedule-title">{copy.schedule}</h2><span>{copy.scheduleIntro}</span></div>
        <div className="rtfm__programs">
          {PROGRAMS.map((program) => {
            const item = program[language];
            const isCurrent = program.id === currentProgram.id;
            return <article key={program.id} className={isCurrent ? 'is-current' : ''}>
              <div className="rtfm__program-art"><img src={`/programs/${program.id}.png`} alt="" /><span>{program.time}</span>{isCurrent ? <b>{copy.current}</b> : null}</div>
              <div className="rtfm__program-copy"><p>{item.days}</p><h3>{item.name}</h3><span>{item.description}</span></div>
            </article>;
          })}
        </div>
      </section>

      <section className="rtfm__principles">
        <article><span>01</span><h2>{copy.sectionTitle}</h2><p>{copy.sectionBody}</p></article>
        <article><span>02</span><h2>{copy.foreverTitle}</h2><p>{copy.foreverBody}</p></article>
      </section>

      <footer className="rtfm__footer"><img src="/brand/radiotedu-logo-white.png" alt="" /><p>RTAI · Ankara · {new Date().getFullYear()}</p></footer>
    </main>
  );
}
