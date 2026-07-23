import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { describe, expect, it, vi } from 'vitest';

import { Dashboard } from '../components/Dashboard';
import { PublicDashboard } from '../components/PublicDashboard';
import type { PublicStatusResponse, StationPublicStatusResponse, StatusResponse } from '../api';

const emptyStatus: StatusResponse = {
  channel: {
    id: 'radiotedu',
    name: 'RadioTEDU',
    description: 'Local AI radio running on your machine.',
    host_model: 'qwen2.5:0.5b-instruct',
    status: 'idle',
    cover_path: '/static/generated/covers/radiotedu_station.png',
  },
  now_playing: {
    type: 'idle',
    title: 'Idle — waiting for music library.',
    artist: null,
    started_at: null,
  },
  queue: [],
  current_program: {
    id: 'night_lab',
    name: 'Jazz Lab',
    description: 'Evening selections with deeper jazz context, experiments, and smart transitions.',
    vibe: 'late jazz',
    host_name: 'Selin',
    host_gender: 'female',
    voice: 'tr_female_cool',
    personality: 'cool, informed, playful, music-first',
    start_time: '18:00',
    end_time: '23:00',
    days_of_week: 'mon,tue',
    cover_path: '/static/generated/covers/night_lab.png',
    active: 1,
  },
  programs: [
    {
      id: 'night_lab',
      name: 'Jazz Lab',
      description: 'Evening selections with deeper jazz context, experiments, and smart transitions.',
      vibe: 'late jazz',
      host_name: 'Selin',
      host_gender: 'female',
      voice: 'tr_female_cool',
      personality: 'cool, informed, playful, music-first',
      start_time: '18:00',
      end_time: '23:00',
      days_of_week: 'mon,tue',
      cover_path: '/static/generated/covers/night_lab.png',
      active: 1,
    },
  ],
  metrics: {
    local_listeners: null,
    popularity: null,
    average_session: null,
    feedback_count: 0,
  },
  top_songs: [],
  top_genres: [],
  listener_messages: [
    {
      content: 'more mellow piano at night',
      source: 'dashboard',
      created_at: '2026-07-05T12:00:00+00:00',
    },
  ],
  incidents: [
    {
      id: 1,
      component: 'llm',
      severity: 'warning',
      status: 'open',
      summary: 'Ollama runtime is unreachable.',
      created_at: '2026-07-06T00:00:00+00:00',
      updated_at: '2026-07-06T00:00:00+00:00',
    },
  ],
  autonomous_tasks: [
    {
      id: 1,
      task_type: 'restart_llm_runtime',
      component: 'llm',
      title: 'Restart Ollama',
      status: 'queued',
      priority: 80,
      attempts: 0,
      created_at: '2026-07-06T00:00:00+00:00',
      updated_at: '2026-07-06T00:00:00+00:00',
    },
  ],
  logs: [],
  health: {
    database: 'ok',
    llm: 'qwen3.5:4b',
    llm_runtime: {
      provider: 'ollama',
      configured_model: 'qwen3.5:4b',
      base_url: 'http://127.0.0.1:11434',
      reachable: false,
      model_available: false,
      installed_models: [],
      status: 'unreachable',
      error: 'connection refused',
    },
    llm_setup: {
      provider: 'ollama',
      configured_model: 'qwen3.5:4b',
      base_url: 'http://127.0.0.1:11434',
      cli_found: false,
      cli_path: null,
      server_reachable: false,
      reachable: false,
      model_available: false,
      installed_models: [],
      status: 'cli_missing',
      summary: 'Ollama CLI was not found.',
      suggested_commands: ['winget install Ollama.Ollama', 'ollama pull qwen3.5:4b'],
      error: 'connection refused',
    },
    tts: 'dummy',
    tts_runtime: {
      provider: 'qwen',
      active_provider: 'dummy',
      status: 'fallback',
      configured: false,
      command_configured: false,
      last_error: null,
    },
    search: 'ok',
    weather: 'disabled',
    playback: 'simulate',
    website_sync: 'not_configured',
  },
  weather: {
    available: false,
    location: 'Ankara',
    summary: 'No weather data.',
    temperature_c: null,
    humidity_percent: null,
    wind_kmh: null,
    condition: null,
    source: 'disabled',
  },
  observability: {
    uptime_seconds: 42,
    announcement_prebuffer: {
      ready: 0,
      used: 0,
      failed: 0,
      required: 5,
      target: 8,
      ready_to_broadcast: false,
      oldest_ready_age_seconds: null,
      next_announcement_type: null,
    },
    generated_clips: 0,
    recent_errors: [],
    supervisor_restarts: 0,
    playback_now: {
      type: 'idle',
      title: 'Idle — waiting for music library.',
      artist: null,
      started_at: null,
    },
    news: {
      enabled: true,
      last_checked_at: '2026-07-06T00:00:00+00:00',
      last_source_at: '2026-07-06T00:00:00+00:00',
      last_source_title: 'Campus observatory opens tonight',
      max_age_hours: 24,
    },
  },
  orchestrator: {
    running: false,
    last_tick_at: null,
    last_strategy_at: null,
    last_error: null,
    strategy: 'RadioTEDU long-horizon strategy: keep one local jazz-first channel.',
    strategy_policy: {
      single_channel: true,
      library_tracks: 0,
      goals: ['Keep RadioTEDU as one channel', 'Grow useful listener memory'],
      next_actions: ['Add music or rescan the library', 'Keep prepared announcements ready'],
      constraints: ['No invented analytics'],
    },
    strategy_revision: 1,
    memory_count: 0,
    draft_count: 0,
    self_review: 'Self-review: no recent plays yet.',
  },
  liquidsoap: {
    enabled: true,
    health: 'missing',
    command: 'liquidsoap',
    command_found: false,
    command_path: null,
    running: false,
    pid: null,
    rendered: true,
    script_path: 'data/liquidsoap/radiotedu.liq',
    queue_path: 'data/liquidsoap/queue.m3u',
    queue_exists: true,
    queue_length: 0,
    mount: '/ai',
    icecast_url: 'http://127.0.0.1:8001/ai',
    icecast_reachable: false,
    mount_active: false,
    icecast_status: null,
    icecast_error: 'connection refused',
  },
  music_library: {
    total_indexed_tracks: 0,
    playable_track_count: 0,
    last_scan_time: null,
  },
  air_readiness: {
    ready: false,
    reason: 'no_music',
    readiness: {
      checklist: {
        music_library: { ok: false, detail: '0 playable tracks indexed.', severity: 'blocking' },
      },
      blocking_failures: ['music_library'],
    },
  },
  maintenance: {
    generated_clip_count: 0,
    agent_log_count: 0,
    last_maintenance: null,
  },
  watchdog: {
    stale_prebuffer: 0,
    ready_prebuffer: 0,
    error_log_count: 0,
    liquidsoap_process_down: 1,
    icecast_mount_down: 1,
    stream_health: 'missing',
    icecast_status: null,
    stuck_playback: 1,
    elapsed_seconds: 42,
    threshold_seconds: 30,
    title: 'Blue Room',
  },
  configuration: {
    MUSIC_DIR: 'data/music',
    OLLAMA_MODEL: 'qwen3.5:4b',
    TTS_COMMAND: 'dummy',
    LIQUIDSOAP_PATH: 'liquidsoap',
    LIQUIDSOAP_SCRIPT: 'data/liquidsoap/radiotedu.liq',
    ICECAST_URL: 'http://127.0.0.1:8001/ai',
    ICECAST_MOUNT: '/ai',
    PUBLIC_SYNC_URL: '',
    PUBLIC_STREAM_URL: '',
    BUFFER_SIZES: { min: 5, max: 8 },
  },
  website_sync: {
    configured: false,
    health: 'not_configured',
    public_sync_url: '',
    public_stream_url: '',
    interval_seconds: 10,
  },
  setup: {
    has_music: false,
    message: 'No music library found. Add audio files to data/music and click Rescan.',
  },
  fallback_playlist: {
    channel_id: 'radiotedu',
    count: 1,
    tracks: [
      {
        id: 1,
        title: 'Blue Room',
        artist: 'Alice',
        genre: 'Jazz',
        duration_seconds: 120,
        file_exists: true,
      },
    ],
  },
  schedule_week: {
    channel_id: 'radiotedu',
    days: [
      { day: 'mon', programs: [{ id: 'night_lab', name: 'Jazz Lab', start_time: '18:00', end_time: '23:00', host_name: 'Selin', vibe: 'late jazz' }] },
      { day: 'tue', programs: [] },
      { day: 'wed', programs: [] },
      { day: 'thu', programs: [] },
      { day: 'fri', programs: [] },
      { day: 'sat', programs: [] },
      { day: 'sun', programs: [] },
    ],
  },
};

describe('Dashboard', () => {
  it('shows one RadioTEDU setup dashboard without invented data', () => {
    render(<Dashboard status={emptyStatus} onRefresh={() => undefined} />);

    expect(screen.getByRole('heading', { name: 'RadioTEDU' })).toBeInTheDocument();
    expect(screen.getByText('Local only operator app')).toBeInTheDocument();
    expect(screen.getByText('Status Lights')).toBeInTheDocument();
    expect(screen.getByText('Website sync')).toBeInTheDocument();
    expect(screen.getByText('Idle — waiting for music library.')).toBeInTheDocument();
    expect(screen.getByText('No music library found. Add audio files to data/music and click Rescan.')).toBeInTheDocument();
    expect(screen.getByText('Queue is empty.')).toBeInTheDocument();
    expect(screen.getByText('No logs yet.')).toBeInTheDocument();
    expect(screen.getByText('Long-Horizon Strategy')).toBeInTheDocument();
    expect(screen.getByText('RadioTEDU long-horizon strategy: keep one local jazz-first channel.')).toBeInTheDocument();
    expect(screen.getByText('Keep RadioTEDU as one channel')).toBeInTheDocument();
    expect(screen.getByText('Add music or rescan the library')).toBeInTheDocument();
    expect(screen.queryByText('Listener Notes')).toBeNull();
    expect(screen.queryByText('more mellow piano at night')).toBeNull();
    expect(screen.getByText('Autonomy Ops')).toBeInTheDocument();
    expect(screen.getByText('Ollama runtime is unreachable.')).toBeInTheDocument();
    expect(screen.getByText('Restart Ollama')).toBeInTheDocument();
    expect(screen.getByText('Air Output')).toBeInTheDocument();
    expect(screen.getByText('Air Readiness')).toBeInTheDocument();
    expect(screen.getByText('0 playable tracks indexed.')).toBeInTheDocument();
    expect(screen.getAllByText('TTS').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('fallback / dummy')).toBeInTheDocument();
    expect(screen.getByText('Maintenance')).toBeInTheDocument();
    expect(screen.getByText('Watchdog')).toBeInTheDocument();
    expect(screen.getByText('Selin / female')).toBeInTheDocument();
    expect(screen.getByText('tr_female_cool')).toBeInTheDocument();
    expect(screen.getAllByText('/ai').length).toBeGreaterThanOrEqual(1);
    expect(screen.getAllByRole('button', { name: 'Run Air' }).length).toBeGreaterThanOrEqual(2);
    expect(screen.getByRole('button', { name: 'Stop Air' })).toBeInTheDocument();
    expect(screen.getByText('Start Icecast Air')).toBeInTheDocument();
    expect(screen.getByText('Playback Watch')).toBeInTheDocument();
    expect(screen.getByText('Stuck: Blue Room')).toBeInTheDocument();
    expect(screen.queryByText('Weather')).toBeNull();
    expect(screen.queryByText('No weather data.')).toBeNull();
    expect(screen.getByText('Runtime Watch')).toBeInTheDocument();
    expect(screen.getByText('Weekly Strategy')).toBeInTheDocument();
    expect(screen.getByText('Emergency Playlist')).toBeInTheDocument();
    expect(screen.getAllByText('Blue Room').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByText('Mobile Operator')).toBeInTheDocument();
    expect(screen.getAllByText('Admin Auth').length).toBeGreaterThanOrEqual(1);
    expect(screen.getByRole('button', { name: 'Verify Icecast Air' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Clip Latest Segment' })).toBeInTheDocument();
    expect(screen.getByText('unreachable (qwen3.5:4b)')).toBeInTheDocument();
    expect(screen.getByText('cli_missing: ollama pull qwen3.5:4b')).toBeInTheDocument();
    expect(screen.getByText('0/5')).toBeInTheDocument();
    expect(screen.getByText('0 / 8')).toBeInTheDocument();
    expect(screen.getByText('minimum 5')).toBeInTheDocument();
    expect(screen.getByText('News Source')).toBeInTheDocument();
    expect(screen.getByText('Campus observatory opens tonight')).toBeInTheDocument();
    expect(screen.getByText('Next Announcement')).toBeInTheDocument();
    expect(screen.getByText('Edit')).toBeInTheDocument();
    expect(screen.queryByText(/support|balance|money|donation|payment|revenue|profit/i)).toBeNull();
    expect(screen.queryByText(/OpenAIR|Grok and Roll|Backlink Broadcast|Thinking Frequencies/i)).toBeNull();
  });

  it('shows local operator controls, configuration, library stats, and log filters', async () => {
    const user = userEvent.setup();
    const operatorStatus = {
      ...emptyStatus,
      logs: [
        {
          level: 'info',
          message: 'Music scan complete',
          created_at: '2026-07-07T08:29:00+00:00',
          metadata: {},
        },
        {
          level: 'error',
          message: 'Icecast unreachable',
          created_at: '2026-07-07T08:30:00+00:00',
          metadata: {},
        },
      ],
      music_library: {
        total_indexed_tracks: 1,
        playable_track_count: 1,
        last_scan_time: '2026-07-07T08:29:00+00:00',
      },
      configuration: {
        MUSIC_DIR: 'F:/Songs/Jazz',
        OLLAMA_MODEL: 'qwen3.5:4b',
        TTS_COMMAND: 'python scripts/qwen_tts_command.py',
        LIQUIDSOAP_SCRIPT: 'liquidsoap/radiotedu.liq',
        ICECAST_URL: 'http://127.0.0.1:8000/ai',
        ICECAST_MOUNT: '/ai',
        PUBLIC_SYNC_URL: 'https://radiotedu.com/api/public/snapshot',
        PUBLIC_STREAM_URL: 'https://radiotedu.com/ai/stream',
        BUFFER_SIZES: { min: 5, max: 8 },
      },
      website_sync: {
        configured: true,
        health: 'configured',
        public_sync_url: 'https://radiotedu.com/api/public/snapshot',
        public_stream_url: 'https://radiotedu.com/ai/stream',
        interval_seconds: 10,
      },
    } as unknown as StatusResponse;

    render(<Dashboard status={operatorStatus} onRefresh={() => undefined} />);

    expect(screen.getByRole('button', { name: 'Say Now' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Generate Covers' })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Rescan Music' })).toBeInTheDocument();
    expect(screen.getByText('Music Library')).toBeInTheDocument();
    expect(screen.getByText('Total Indexed Tracks')).toBeInTheDocument();
    expect(screen.getByText('Playable Tracks')).toBeInTheDocument();
    expect(screen.getByText('Configuration')).toBeInTheDocument();
    expect(screen.getByText('MUSIC_DIR')).toBeInTheDocument();
    expect(screen.getByText('F:/Songs/Jazz')).toBeInTheDocument();
    expect(screen.getByText('PUBLIC_SYNC_URL')).toBeInTheDocument();
    expect(screen.getByText('Website Sync')).toBeInTheDocument();
    expect(screen.getByText('https://radiotedu.com/ai/stream')).toBeInTheDocument();

    await user.click(screen.getByRole('button', { name: 'Errors' }));

    expect(screen.getByText('Icecast unreachable')).toBeInTheDocument();
    expect(screen.queryByText('Music scan complete')).toBeNull();
  });

  it('sends manual announcements and cover generation through local admin APIs', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ queued: true }),
    } as unknown as Response);

    render(<Dashboard status={emptyStatus} onRefresh={() => undefined} />);

    await user.click(screen.getByRole('button', { name: 'Say Now' }));
    await user.type(screen.getByLabelText('Announcement text'), 'A short RadioTEDU bulletin');
    await user.click(screen.getByRole('button', { name: 'Send announcement' }));
    await user.click(screen.getByRole('button', { name: 'Generate Covers' }));
    await user.click(screen.getByRole('button', { name: 'Test TTS' }));
    await user.click(screen.getByRole('button', { name: 'Run Maintenance' }));

    expect(fetchMock).toHaveBeenCalledWith('/api/control/say', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ text: 'A short RadioTEDU bulletin' }),
    });
    expect(fetchMock).toHaveBeenCalledWith('/api/art/generate-program-covers', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    });
    expect(fetchMock).toHaveBeenCalledWith('/api/tts/test', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ program_id: 'night_lab' }),
    });
    expect(fetchMock).toHaveBeenCalledWith('/api/maintenance/run', {
      method: 'POST',
      headers: undefined,
      body: undefined,
    });
    fetchMock.mockRestore();
  });

  it('shows broadcast startup failures inline', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({
        started: false,
        stream: {
          reason: 'liquidsoap_missing',
          icecast_url: 'http://127.0.0.1:8001/ai',
        },
      }),
    } as unknown as Response);

    render(<Dashboard status={emptyStatus} onRefresh={() => undefined} />);
    await user.click(screen.getAllByRole('button', { name: 'Run Air' })[1]);

    expect(await screen.findByText('Run Air cannot start yet: Liquidsoap is not installed or not in PATH. Icecast mount is configured as http://127.0.0.1:8001/ai.')).toBeInTheDocument();
    fetchMock.mockRestore();
  });

  it('supports keyboard-safe Run, Stop, and Skip controls', async () => {
    const user = userEvent.setup();
    const confirmMock = vi.spyOn(window, 'confirm').mockReturnValue(true);
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue({
      ok: true,
      json: () => Promise.resolve({ started: true }),
    } as unknown as Response);

    render(<Dashboard status={emptyStatus} onRefresh={() => undefined} />);
    await user.keyboard('rsk');

    expect(fetchMock).toHaveBeenCalledWith('/api/air/start', expect.anything());
    expect(fetchMock).toHaveBeenCalledWith('/api/air/stop', expect.anything());
    expect(fetchMock).toHaveBeenCalledWith('/api/control/skip', expect.anything());
    fetchMock.mockRestore();
    confirmMock.mockRestore();
  });

  it('requires confirmation for disruptive air controls', async () => {
    const user = userEvent.setup();
    const confirmMock = vi.spyOn(window, 'confirm').mockReturnValue(false);
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true } as Response);

    render(<Dashboard status={emptyStatus} onRefresh={() => undefined} />);
    await user.click(screen.getByRole('button', { name: 'Stop Air' }));
    await user.click(screen.getByRole('button', { name: 'Skip Track' }));
    await user.click(screen.getByRole('button', { name: 'Rescan Music' }));

    expect(confirmMock).toHaveBeenCalledTimes(3);
    expect(fetchMock).not.toHaveBeenCalled();
    confirmMock.mockRestore();
    fetchMock.mockRestore();
  });

  it('organizes admin panels as a compact broadcast operations workspace', () => {
    render(<Dashboard status={emptyStatus} onRefresh={() => undefined} />);

    const operations = screen.getByLabelText('Broadcast operations');
    expect(operations).toHaveClass('admin-panel-deck');
    expect(operations).toContainElement(screen.getByText('Air Readiness'));
    expect(operations).toContainElement(screen.getByText('Air Output'));
    expect(operations).toContainElement(screen.getByText('Runtime Watch'));
  });

  it('sends program edits through the API', async () => {
    const user = userEvent.setup();
    const fetchMock = vi.spyOn(globalThis, 'fetch').mockResolvedValue({ ok: true } as Response);

    render(<Dashboard status={emptyStatus} onRefresh={() => undefined} />);
    await user.click(screen.getAllByText('Edit')[0]);
    await user.clear(screen.getByLabelText('Start time'));
    await user.type(screen.getByLabelText('Start time'), '19:00');
    await user.clear(screen.getByLabelText('End time'));
    await user.type(screen.getByLabelText('End time'), '23:30');
    await user.clear(screen.getByLabelText('Days'));
    await user.type(screen.getByLabelText('Days'), 'fri,sat');
    await user.clear(screen.getByLabelText('Vibe'));
    await user.type(screen.getByLabelText('Vibe'), 'deep nocturnal jazz');
    await user.clear(screen.getByLabelText('Host name'));
    await user.type(screen.getByLabelText('Host name'), 'Selin');
    await user.clear(screen.getByLabelText('Host gender'));
    await user.type(screen.getByLabelText('Host gender'), 'female');
    await user.clear(screen.getByLabelText('Voice id'));
    await user.type(screen.getByLabelText('Voice id'), 'tr_female_cool');
    await user.clear(screen.getByLabelText('Personality'));
    await user.type(screen.getByLabelText('Personality'), 'cool, informed, playful');
    await user.click(screen.getByRole('button', { name: 'Save program' }));

    expect(fetchMock).toHaveBeenCalledWith('/api/programs/night_lab', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        start_time: '19:00',
        end_time: '23:30',
        days_of_week: 'fri,sat',
        vibe: 'deep nocturnal jazz',
        host_name: 'Selin',
        host_gender: 'female',
        voice: 'tr_female_cool',
        personality: 'cool, informed, playful',
      }),
    });
    fetchMock.mockRestore();
  });
});

const publicStatus: PublicStatusResponse = {
  online: false,
  schema_version: 1,
  received_at: null,
  generated_at: null,
  expires_at: null,
  message: 'Waiting for the broadcast computer to sync.',
  channel: {
    id: 'radiotedu',
    name: 'RadioTEDU',
    description: 'AI radio from RadioTEDU.',
    host_model: 'qwen3.5:4b',
    status: 'idle',
    cover_path: '/static/generated/covers/radiotedu_station.png',
  },
  now_playing: {
    type: 'idle',
    title: 'Waiting for RadioTEDU broadcast.',
    artist: null,
    started_at: null,
  },
  current_program: {
    id: 'night_lab',
    name: 'Jazz Lab',
    description: 'Evening selections with deeper jazz context, experiments, and smart transitions.',
    vibe: 'late jazz',
    host_name: 'Selin',
    host_gender: 'female',
    voice: 'tr_female_cool',
    personality: 'cool, informed, playful, music-first',
    start_time: '18:00',
    end_time: '23:00',
    days_of_week: 'mon,tue',
    cover_path: '/static/generated/covers/night_lab.png',
    active: 1,
  },
  next_programs: [],
  programs: [
    {
      id: 'night_lab',
      name: 'Jazz Lab',
      description: 'Evening selections with deeper jazz context, experiments, and smart transitions.',
      vibe: 'late jazz',
      host_name: 'Selin',
      host_gender: 'female',
      voice: 'tr_female_cool',
      personality: 'cool, informed, playful, music-first',
      start_time: '18:00',
      end_time: '23:00',
      days_of_week: 'mon,tue',
      cover_path: '/static/generated/covers/night_lab.png',
      active: 1,
    },
  ],
  top_songs: [{ id: 1, title: 'Blue Room', artist: 'Alice', plays: 3 }],
  top_genres: [{ genre: 'Jazz', plays: 3 }],
  stream: { url: 'https://radiotedu.com/live.mp3', status: 'configured' },
  current_minutes_left: 42,
  next_program: {
    id: 'morning_signal',
    name: 'TEDU Dawn',
    description: 'A gentle campus morning handoff.',
    vibe: 'morning jazz',
    host_name: 'Ece',
    host_gender: 'female',
    voice: 'tr_female_warm',
    personality: 'warm, precise',
    start_time: '06:00',
    end_time: '10:00',
    days_of_week: 'mon,tue,wed,thu,fri',
    cover_path: '/static/generated/covers/morning_signal.png',
    active: 1,
  },
  content_breakdown: [
    { label: 'Music', percent: 84 },
    { label: 'Talking', percent: 16 },
  ],
  activity: [
    {
      kind: 'listener',
      actor: 'Listener',
      content: 'more mellow piano after midnight',
      created_at: '2026-07-06T00:02:00+00:00',
    },
    {
      kind: 'broadcast',
      actor: 'RadioTEDU',
      content: 'Queued Blue Room by Alice.',
      created_at: '2026-07-06T00:03:00+00:00',
    },
  ],
  metrics: {
    current_listeners: 0,
    popularity: null,
    average_session: null,
  },
  share_card: {
    title: 'RadioTEDU: Blue Room',
    text: 'Blue Room by Alice',
    url: 'https://radiotedu.com/live.mp3',
    image: '/static/generated/covers/radiotedu_station.png',
  },
};

const stationPublicStatus: StationPublicStatusResponse = {
  protocol: 'radiotedu-platform/v1',
  station_id: 'radiotedu-en',
  online: true,
  stale: false,
  received_at: '2026-07-15T10:00:01Z',
  snapshot: {
    protocol: 'radiotedu-platform/v1',
    schema_version: 2,
    station: { id: 'radiotedu-en', language: 'en', display_name: 'RadioTEDU English' },
    sequence: 12,
    generated_at: '2026-07-15T10:00:00Z',
    expires_at: null,
    operational_state: 'live',
    speech_state: { active: false, kind: 'music' },
    now_playing: {
      kind: 'music',
      track_id: 'track-12',
      title: 'Blue Room',
      artist: 'Alice',
      cover_id: null,
      mood: 'calm',
      sound_tags: ['calm'],
      started_at: '2026-07-15T09:58:00Z',
    },
    current_program: { id: 'jazz-lab', name: 'Jazz Lab', vibe: 'Warm focus', sound_tags: ['warm', 'focused'] },
    next_program: { id: 'tedu-dawn', name: 'TEDU Dawn', vibe: 'Bright start', sound_tags: ['bright'] },
    stream: {
      url: 'https://stream.radiotedu.com/ai',
      mount: '/en',
      status: 'live',
      codec: 'AAC-LC',
      bitrate_kbps: 192,
      public: true,
    },
    editorial: { sound_tags: ['warm', 'calm', 'focused'] },
  },
  metrics: {
    active_website_listeners: 17,
    airtime: { window_days: 14, classified_duration_ms: 123_000, music_percent: 84, talking_percent: 16 },
  },
};

describe('PublicDashboard', () => {
  it('renders only the essential English listener information', () => {
    render(<PublicDashboard status={stationPublicStatus} language="en" />);

    expect(screen.getByRole('heading', { name: 'RadioTEDU AI' })).toBeInTheDocument();
    expect(screen.getAllByRole('heading', { name: 'Blue Room' })).toHaveLength(2);
    expect(screen.getByRole('img', { name: 'RadioTEDU' })).toHaveAttribute(
      'src',
      '/brand/radiotedu-logo-white.png',
    );
    expect(screen.getByRole('img', { name: 'RTAI' })).toBeInTheDocument();
    expect(screen.getByLabelText('RadioTEDU English live stream')).toHaveAttribute(
      'src',
      'https://stream.radiotedu.com/ai',
    );
    expect(screen.getByLabelText('RadioTEDU English listener')).toContainElement(
      screen.getByLabelText('RadioTEDU English live stream'),
    );
    expect(screen.getByLabelText('RadioTEDU English listener')).toHaveTextContent('Current listeners');
    expect(screen.getAllByText('Blue Room').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Jazz Lab').length).toBeGreaterThan(0);
    expect(screen.getAllByText('TEDU Dawn').length).toBeGreaterThan(0);
    expect(screen.getByText('17')).toBeInTheDocument();
    expect(screen.getByText('84%')).toBeInTheDocument();
    expect(screen.getByText('16%')).toBeInTheDocument();
    expect(screen.getByText('Warm')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'EN' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'FR' })).toHaveAttribute('aria-pressed', 'false');
    expect(screen.queryByText(/contact|message|purchase|wallet|reward|vote|social|playout|control|copy|share/i)).toBeNull();
  });

  it('localizes the French station and language switch', () => {
    const frenchStatus: StationPublicStatusResponse = {
      ...stationPublicStatus,
      station_id: 'radiotedu-fr',
      snapshot: {
        ...stationPublicStatus.snapshot!,
        station: { id: 'radiotedu-fr', language: 'fr', display_name: 'RadioTEDU Français' },
        stream: { ...stationPublicStatus.snapshot!.stream, url: 'https://stream.radiotedu.com/event', mount: '/fr' },
      },
    };

    render(<PublicDashboard status={frenchStatus} language="fr" />);

    expect(screen.getAllByRole('heading', { name: 'Blue Room' })).toHaveLength(2);
    expect(screen.getByLabelText('Flux en direct de RadioTEDU Français')).toHaveAttribute(
      'src',
      'https://stream.radiotedu.com/event',
    );
    expect(screen.getAllByText('Émission actuelle').length).toBeGreaterThan(0);
    expect(screen.getByText('Musique')).toBeInTheDocument();
    expect(screen.getByText('Parlé')).toBeInTheDocument();
    expect(screen.getByText('Chaleureux')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'FR' })).toHaveAttribute('aria-pressed', 'true');
  });

  it('shows unavailable instead of a zero split when classified airtime is empty', () => {
    const emptyStatus: StationPublicStatusResponse = {
      ...stationPublicStatus,
      metrics: {
        ...stationPublicStatus.metrics,
        airtime: { window_days: 14, classified_duration_ms: 0, music_percent: null, talking_percent: null },
      },
    };

    render(<PublicDashboard status={emptyStatus} language="en" />);

    expect(screen.getByLabelText('Last 14 days')).toHaveTextContent('Unavailable');
    expect(screen.queryByText('0%')).toBeNull();
  });

  it('keeps the last valid snapshot visible when polling is interrupted', () => {
    render(
      <PublicDashboard
        status={{ ...stationPublicStatus, stale: true }}
        language="en"
        connectionError="Station status request failed: 503"
      />,
    );

    expect(screen.getAllByText('Blue Room').length).toBeGreaterThan(0);
    expect(screen.getByRole('status')).toHaveTextContent(
      'Live data connection interrupted. Showing the last valid station snapshot.',
    );
    expect(screen.queryByText('Station status request failed: 503')).toBeNull();
  });
});
