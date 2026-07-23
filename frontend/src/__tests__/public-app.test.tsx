import { cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
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
      station: { id: stationId, language, display_name: language === 'en' ? 'RadioTEDU English' : 'RadioTEDU Français' },
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

describe('public listener route', () => {
  it('maps the single /ai route to the English station by default', async () => {
    window.history.replaceState({}, '', '/ai');
    fetchStation.mockResolvedValue(statusFor('radiotedu-en'));

    const view = render(<App />);

    expect(await screen.findByRole('img', { name: 'RadioTEDU' })).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'RadioTEDU AI' })).toBeInTheDocument();
    const listener = screen.getByLabelText('RadioTEDU English listener');
    expect(within(listener).getByText('Current listeners')).toBeInTheDocument();
    expect(within(listener).getByText('0')).toBeInTheDocument();
    expect(within(listener).getByRole('button', { name: 'Listen live' })).toBeInTheDocument();
    expect(fetchStation).toHaveBeenCalledWith('radiotedu-en');
    await waitFor(() => expect(postSession).toHaveBeenCalledWith('radiotedu-en', 'start', expect.any(String)));

    view.unmount();
    expect(postSession).toHaveBeenCalledWith('radiotedu-en', 'end', expect.any(String), true);
  });

  it('switches to French in place without creating another page route', async () => {
    const user = userEvent.setup();
    window.history.replaceState({}, '', '/ai');
    fetchStation.mockImplementation(async (stationId) => statusFor(stationId));

    render(<App />);
    await screen.findByRole('button', { name: 'FR' });
    await user.click(screen.getByRole('button', { name: 'FR' }));

    await waitFor(() => expect(fetchStation).toHaveBeenCalledWith('radiotedu-fr'));
    expect(window.location.pathname).toBe('/ai');
    expect(screen.getByRole('button', { name: 'FR' })).toHaveAttribute('aria-pressed', 'true');
  });
});
