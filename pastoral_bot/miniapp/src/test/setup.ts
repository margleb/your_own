import '@testing-library/jest-dom/vitest';
import { afterEach, beforeEach, vi } from 'vitest';
import { cleanup } from '@testing-library/react';

beforeEach(() => {
  window.Telegram = undefined;
  Object.defineProperty(window, 'scrollTo', { value: vi.fn(), configurable: true });
  Element.prototype.scrollIntoView = vi.fn();
});
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.useRealTimers(); });
