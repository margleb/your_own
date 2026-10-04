import { describe, expect, it, vi } from 'vitest';
import { PastoralApi, safeSourceUrl } from '../api';

describe('Telegram API client', () => {
  it('uses signed authorization on GET and POST, relative API paths, and a stable request UUID', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({}) });
    vi.stubGlobal('fetch', fetcher);
    const api = new PastoralApi('signed-telegram-data');
    await api.state();
    await api.send('89f0a812-0231-4b0b-aaae-1860d093c653', 'Вопрос', 12);
    await api.job('89f0a812-0231-4b0b-aaae-1860d093c653');
    for (const [, options] of fetcher.mock.calls) {
      expect(options.headers.Authorization).toBe('tma signed-telegram-data');
      expect(options.cache).toBe('no-store');
      expect(options.credentials).toBe('omit');
    }
    expect(fetcher.mock.calls[0][0].pathname).toBe('/api/state');
    expect(JSON.parse(fetcher.mock.calls[1][1].body)).toEqual({ request_id: '89f0a812-0231-4b0b-aaae-1860d093c653', text: 'Вопрос', expected_epoch: 12 });
  });
  it('does not contact the backend without initData', async () => {
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    await expect(new PastoralApi('').state()).rejects.toMatchObject({ code: 'unauthorized', status: 401 });
    expect(fetcher).not.toHaveBeenCalled();
  });
  it('turns expired authorization into a safe reopening instruction', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: false, status: 401, json: async () => ({ error: { code: 'other', message: 'internal auth details' } }) }));
    await expect(new PastoralApi('expired').state()).rejects.toMatchObject({ code: 'unauthorized', message: 'Срок доступа истёк. Откройте приложение заново из бота.' });
  });
  it('rejects script, insecure and credential-bearing source links', () => {
    expect(safeSourceUrl('javascript:alert(1)')).toBeNull();
    expect(safeSourceUrl('http://azbyka.ru/test')).toBeNull();
    expect(safeSourceUrl('https://user:secret@azbyka.ru/test')).toBeNull();
    expect(safeSourceUrl('https://azbyka.ru/biblia/?Mt.6')).toBe('https://azbyka.ru/biblia/?Mt.6');
  });
});
