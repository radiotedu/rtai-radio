import { cleanup, render, screen, waitFor } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import App from '../App';
import type { StationId, StationPublicStatusResponse } from '../api';
import { fetchStationPublicStatus, postStationPublicSession } from '../api';

vi.mock('../api', async (importOriginal) => {
  const actual = await importOriginal<typeof import('../api')>();
  return {
    ...actual,
    fetchStatus: vi.fn(),
    fetchStationPublicStatus: vi.fn(),
    postStationPublicSession: vi.fn().mockResolvedValue(undefined),
  };
});

const fetchStation = vi.mocked(fetchStationPublicStatus);
const postSession = vi.mocked(postStationPublicSession);

function statusFor(stationId: StationId): StationPublicStatusResponse {
  const language = stationId === 'radiotedu-en' ? 'en' : 'fr';
  const mount = language === 'en' ? '/en' : '/fr';
  return {
    protocol: 'radiotedu-platform/v1',
    station_id: stationId,
    online: true,
    stale: false,
    received_at: '2026-07-15T10:00:01Z',
    snapshot: {
      protocol: 'radiotedu-platform/v1',
      schema_version: 2,
      station: { id: stationId, language, display_name: `RadioTEDU ${language}` },
      sequence: 1,
      generated_at: '2026-07-15T10:00:00Z',
      expires_at: null,
      operational_state: 'live',
      speech_state: { active: false, kind: 'music' },
      now_playing: null,
      current_program: null,
      next_program: null,
      stream: {
        url: `https://stream.radiotedu.com${mount}`,
        mount,
        status: 'live',
        codec: 'AAC-LC',
        bitrate_kbps: 192,
        public: true,
      },
      editorial: { sound_tags: [] },
    },
    metrics: {
      active_website_listeners: 0,
      airtime: { window_days: 14, classified_duration_ms: 0, music_percent: null, talking_percent: null },
    },
  };
}

afterEach(() => {
  cleanup();
  vi.clearAllMocks();
  window.history.replaceState({}, '', '/');
});

describe('public listener routes', () => {
  it.each([
    ['/ai', 'radiotedu-en', 'RadioTEDU English'],
    ['/ai/en', 'radiotedu-en', 'RadioTEDU English'],
    ['/ai/fr', 'radiotedu-fr', 'RadioTEDU Français'],
  ] as const)('maps %s to the isolated %s station', async (path, stationId, heading) => {
    window.history.replaceState({}, '', path);
    fetchStation.mockResolvedValue(statusFor(stationId));

    const view = render(<App />);

    expect(await screen.findByRole('heading', { name: heading })).toBeInTheDocument();
    expect(fetchStation).toHaveBeenCalledWith(stationId);
    await waitFor(() => expect(postSession).toHaveBeenCalledWith(stationId, 'start', expect.any(String)));

    view.unmount();
    expect(postSession).toHaveBeenCalledWith(stationId, 'end', expect.any(String), true);
  });
});
