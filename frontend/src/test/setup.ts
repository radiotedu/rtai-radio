import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, vi } from 'vitest';

Object.defineProperties(window.HTMLMediaElement.prototype, {
  pause: { configurable: true, value: vi.fn() },
  load: { configurable: true, value: vi.fn() },
  play: { configurable: true, value: vi.fn().mockResolvedValue(undefined) },
});

afterEach(() => {
  cleanup();
  if (typeof window.localStorage?.clear === 'function') {
    window.localStorage.clear();
  }
});
