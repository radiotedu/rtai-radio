export type ChannelStatus = 'live' | 'idle' | 'stopped' | 'error';

export interface Channel {
  id: string;
  name: string;
  description: string;
  host_model: string;
  status: ChannelStatus;
  cover_path: string | null;
}

export interface PlaybackItem {
  type: string;
  title: string;
  artist: string | null;
  file_path?: string;
  started_at: string | null;
}

export interface Program {
  id: string;
  name: string;
  description: string;
  vibe: string | null;
  start_time: string;
  end_time: string;
  days_of_week: string;
  cover_path: string | null;
  host_name?: string | null;
  host_gender?: string | null;
  voice?: string | null;
  personality?: string | null;
  active?: number;
}

export interface Metrics {
  local_listeners: number | null;
  popularity: number | null;
  average_session: string | null;
  feedback_count: number;
}

export interface TopSong {
  id: number;
  title: string;
  artist: string;
  plays: number;
}

export interface TopGenre {
  genre: string;
  plays: number;
}

export interface ListenerMessage {
  content: string;
  source: string;
  created_at: string;
}

export interface AutonomyIncident {
  id: number;
  component: string;
  severity: string;
  status: string;
  summary: string;
  created_at: string;
  updated_at: string;
}

export interface AutonomousTask {
  id: number;
  task_type: string;
  component: string;
  title: string;
  status: string;
  priority: number;
  attempts: number;
  created_at: string;
  updated_at: string;
}

export interface AgentLog {
  level: string;
  message: string;
  created_at: string;
  metadata?: Record<string, unknown>;
}

export interface LlmRuntimeHealth {
  provider: string;
  configured_model: string;
  base_url: string;
  reachable: boolean;
  model_available: boolean;
  installed_models: string[];
  status: string;
  error: string | null;
}

export interface LlmSetupHealth extends LlmRuntimeHealth {
  cli_found: boolean;
  cli_path: string | null;
  server_reachable: boolean;
  summary: string;
  suggested_commands: string[];
}

export interface TtsRuntimeHealth {
  provider: string;
  active_provider: string;
  status: string;
  configured: boolean;
  command_configured?: boolean;
  last_error: string | null;
}

export interface SystemHealth {
  database: string;
  llm: string;
  llm_runtime: LlmRuntimeHealth;
  llm_setup: LlmSetupHealth;
  tts: string;
  tts_runtime: TtsRuntimeHealth;
  search: string;
  weather: string;
  playback: string;
  website_sync: string;
}

export interface OrchestratorState {
  running: boolean;
  last_tick_at: string | null;
  last_strategy_at: string | null;
  last_error: string | null;
  strategy: string | null;
  strategy_policy: {
    single_channel: boolean;
    library_tracks: number;
    library_signals?: string;
    listener_memory?: string;
    goals: string[];
    next_actions: string[];
    constraints: string[];
  } | null;
  strategy_revision: number;
  memory_count: number;
  draft_count: number;
  self_review: string | null;
}

export interface RuntimeObservability {
  uptime_seconds: number;
  announcement_prebuffer: {
    ready: number;
    used: number;
    failed: number;
    required: number;
    target: number;
    ready_to_broadcast: boolean;
    oldest_ready_age_seconds: number | null;
    next_announcement_type: string | null;
  };
  generated_clips: number;
  recent_errors: Array<{ message: string; created_at: string }>;
  supervisor_restarts: number;
  playback_now: PlaybackItem;
  news: {
    enabled: boolean;
    last_checked_at: string | null;
    last_source_at: string | null;
    last_source_title: string | null;
    max_age_hours: number;
  };
}

export interface LiquidsoapState {
  enabled: boolean;
  health: string;
  command: string;
  command_found: boolean;
  command_path: string | null;
  running: boolean;
  pid: number | null;
  rendered: boolean;
  script_path: string;
  queue_path: string;
  queue_exists: boolean;
  queue_length: number;
  mount: string;
  icecast_url: string;
  icecast_reachable: boolean;
  mount_active: boolean;
  icecast_status: number | null;
  icecast_error: string | null;
}

export interface SetupState {
  has_music: boolean;
  message: string;
}

export interface MusicLibraryStats {
  total_indexed_tracks: number;
  playable_track_count: number;
  last_scan_time: string | null;
}

export interface AirReadiness {
  ready: boolean;
  reason: string;
  readiness: {
    checklist: Record<string, { ok: boolean; detail: string; severity: string }>;
    blocking_failures?: string[];
  };
}

export interface MaintenanceState {
  generated_clip_count: number;
  agent_log_count: number;
  last_maintenance: { value: string; updated_at: string } | null;
}

export interface WatchdogState {
  stale_prebuffer: number;
  ready_prebuffer: number;
  error_log_count: number;
  liquidsoap_process_down: number;
  icecast_mount_down: number;
  stream_health: string;
  icecast_status: number | null;
  stuck_playback: number;
  elapsed_seconds: number | null;
  threshold_seconds: number | null;
  title: string | null;
}

export interface OperatorConfiguration {
  MUSIC_DIR: string;
  OLLAMA_MODEL: string;
  TTS_COMMAND: string;
  LIQUIDSOAP_PATH: string;
  LIQUIDSOAP_SCRIPT: string;
  ICECAST_URL: string;
  ICECAST_MOUNT: string;
  PUBLIC_SYNC_URL: string;
  PUBLIC_STREAM_URL: string;
  ADMIN_AUTH?: string;
  BUFFER_SIZES: {
    min: number;
    max: number;
  };
}

export interface FallbackPlaylistTrack {
  id: number;
  title: string;
  artist: string;
  genre: string | null;
  duration_seconds: number | null;
  file_exists: boolean;
}

export interface FallbackPlaylist {
  channel_id: string;
  count: number;
  tracks: FallbackPlaylistTrack[];
}

export interface ScheduleWeekProgram {
  id: string;
  name: string;
  start_time: string;
  end_time: string;
  host_name: string | null;
  vibe: string | null;
}

export interface ScheduleWeek {
  channel_id: string;
  days: Array<{ day: string; programs: ScheduleWeekProgram[] }>;
}

export interface WebsiteSyncHealth {
  configured: boolean;
  health: string;
  public_sync_url: string;
  public_stream_url: string;
  interval_seconds: number;
  pusher?: {
    configured: boolean;
    running: boolean;
    last_push_at: number | null;
    last_result: Record<string, unknown> | null;
    consecutive_failures: number;
    interval_seconds: number;
  } | null;
}

export interface WeatherContext {
  available: boolean;
  location: string;
  summary: string;
  temperature_c: number | null;
  humidity_percent: number | null;
  wind_kmh: number | null;
  condition: string | null;
  source: string;
}

export interface StatusResponse {
  channel: Channel;
  now_playing: PlaybackItem;
  queue: PlaybackItem[];
  current_program: Program | null;
  programs: Program[];
  next_programs?: Program[];
  metrics: Metrics;
  top_songs: TopSong[];
  top_genres: TopGenre[];
  listener_messages: ListenerMessage[];
  incidents: AutonomyIncident[];
  autonomous_tasks: AutonomousTask[];
  weather: WeatherContext;
  logs: AgentLog[];
  health: SystemHealth;
  observability: RuntimeObservability;
  orchestrator: OrchestratorState;
  liquidsoap: LiquidsoapState;
  music_library: MusicLibraryStats;
  air_readiness: AirReadiness;
  maintenance: MaintenanceState;
  watchdog: WatchdogState;
  configuration: OperatorConfiguration;
  website_sync: WebsiteSyncHealth;
  fallback_playlist: FallbackPlaylist;
  schedule_week: ScheduleWeek;
  setup: SetupState;
}

export interface PublicStream {
  url: string;
  status: string;
}

export interface PublicMetrics {
  current_listeners: number;
  popularity: number | null;
  average_session: string | null;
}

export interface PublicContentBreakdown {
  label: string;
  percent: number;
}

export interface PublicActivityItem {
  kind: 'listener' | 'host' | 'broadcast' | string;
  actor: string;
  content: string;
  created_at: string | null;
}

export interface PublicStatusResponse {
  online: boolean;
  schema_version: number;
  received_at: string | null;
  generated_at: string | null;
  expires_at: string | null;
  message: string;
  channel: Channel;
  now_playing: PlaybackItem;
  current_program: Program | null;
  current_minutes_left: number | null;
  next_program: Program | null;
  next_programs: Program[];
  programs: Program[];
  top_songs: TopSong[];
  top_genres: TopGenre[];
  content_breakdown: PublicContentBreakdown[];
  activity: PublicActivityItem[];
  stream: PublicStream;
  metrics: PublicMetrics;
  share_card: {
    title: string;
    text: string;
    url: string;
    image: string;
  };
}

export async function fetchStatus(): Promise<StatusResponse> {
  const response = await fetch('/api/status');
  if (!response.ok) {
    throw new Error(`Status request failed: ${response.status}`);
  }
  return response.json() as Promise<StatusResponse>;
}

export async function fetchPublicStatus(): Promise<PublicStatusResponse> {
  const response = await fetch('/api/public/status');
  if (!response.ok) {
    throw new Error(`Public status request failed: ${response.status}`);
  }
  return response.json() as Promise<PublicStatusResponse>;
}

export async function postPublicSession(path: string, sessionId: string): Promise<void> {
  await fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId }),
  });
}

export type StationId = 'radiotedu-en' | 'radiotedu-fr';
export type PublicLanguage = 'en' | 'fr';
export type SoundTag = 'warm' | 'bright' | 'calm' | 'focused' | 'energetic';

export interface StationPublicProgram {
  id: string;
  name: string;
  vibe: string | null;
  sound_tags: SoundTag[];
}

export interface StationPublicSnapshot {
  protocol: 'radiotedu-platform/v1';
  schema_version: 2;
  station: {
    id: StationId;
    language: PublicLanguage;
    display_name: string;
  };
  sequence: number;
  generated_at: string;
  expires_at: string | null;
  operational_state: 'live' | 'degraded' | 'offline' | 'starting' | 'unknown';
  speech_state: {
    active: boolean;
    kind: 'music' | 'talking' | 'idle' | 'unknown';
  };
  now_playing: {
    kind: 'music' | 'talking' | 'imaging' | 'unknown';
    track_id: string | null;
    title: string | null;
    artist: string | null;
    cover_id: string | null;
    mood: string | null;
    sound_tags: SoundTag[];
    started_at: string | null;
  } | null;
  current_program: StationPublicProgram | null;
  next_program: StationPublicProgram | null;
  stream: {
    url: string;
    mount: '/ai' | '/event';
    status: 'live' | 'degraded' | 'offline' | 'unknown';
    codec: 'AAC-LC';
    bitrate_kbps: 192;
    public: true;
  };
  editorial: { sound_tags: SoundTag[] };
}

export interface StationPublicStatusResponse {
  protocol: 'radiotedu-platform/v1';
  station_id: StationId;
  online: boolean;
  stale: boolean;
  received_at: string | null;
  snapshot: StationPublicSnapshot | null;
  metrics: {
    active_website_listeners: number;
    airtime: {
      window_days: 14;
      classified_duration_ms: number;
      music_percent: number | null;
      talking_percent: number | null;
    };
  };
}

export async function fetchStationPublicStatus(stationId: StationId): Promise<StationPublicStatusResponse> {
  const response = await fetch(`/v1/radio/stations/${stationId}/status`);
  if (!response.ok) {
    throw new Error(`Station status request failed: ${response.status}`);
  }
  return response.json() as Promise<StationPublicStatusResponse>;
}

export async function postStationPublicSession(
  stationId: StationId,
  operation: 'start' | 'heartbeat' | 'end',
  sessionId: string,
  keepalive = false,
): Promise<void> {
  await fetch(`/v1/radio/stations/${stationId}/sessions/${operation}`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ session_id: sessionId }),
    keepalive,
  });
}

export async function postControl(path: string, body?: unknown): Promise<Record<string, unknown>> {
  const headers = jsonHeaders(body);
  const response = await fetch(path, {
    method: 'POST',
    headers,
    body: body ? JSON.stringify(body) : undefined,
  });
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
  return response.json() as Promise<Record<string, unknown>>;
}

export async function patchJson(path: string, body: unknown): Promise<void> {
  const response = await fetch(path, {
    method: 'PATCH',
    headers: jsonHeaders(body),
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    throw new Error(`Request failed: ${response.status}`);
  }
}

function jsonHeaders(body?: unknown) {
  const headers: Record<string, string> = {};
  if (body) {
    headers['Content-Type'] = 'application/json';
  }
  try {
    const token = window.localStorage.getItem('radiotedu_admin_token');
    if (token) {
      headers['X-RadioTEDU-Admin-Token'] = token;
    }
  } catch {
    // Local storage may be unavailable in hardened browser contexts.
  }
  return Object.keys(headers).length ? headers : undefined;
}
