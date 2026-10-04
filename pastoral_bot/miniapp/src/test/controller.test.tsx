import { act, renderHook, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { usePastoral } from '../usePastoral';
import type { BotState, Job } from '../types';

export const stateFixture = (changes: Partial<BotState> = {}): BotState => ({
  mode: 'talk', epoch: 1, conversation_id: 'topic-1', history: [],
  remaining_answers: 10, daily_answer_limit: 10, max_message_chars: 6000,
  temporary: { active: false, expired: false, idle_seconds: 1800, max_seconds: 7200, remaining_seconds: 0 },
  ...changes,
});
const response = (payload: unknown) => ({ ok: true, json: async () => payload }) as Response;

describe('conversation controller', () => {
  it('keeps an already completed answer when the following history refresh loses connectivity', async () => {
    let calls = 0;
    vi.stubGlobal('fetch', vi.fn(async (url: URL, options: RequestInit) => {
      if (url.pathname.endsWith('/state')) {
        if (++calls === 1) return response(stateFixture());
        throw new TypeError('history temporarily unavailable');
      }
      const body = JSON.parse(options.body as string);
      return response({ request_id: body.request_id, status: 'done', reply: { text: 'Полученный ответ', sources: [] } });
    }));
    const { result } = renderHook(() => usePastoral('signed'));
    await waitFor(() => expect(result.current.state).not.toBeNull());
    act(() => result.current.send('Вопрос перед потерей связи'));
    await waitFor(() => expect(result.current.messages.at(-1)?.text).toBe('Полученный ответ'));
    expect(result.current.messages.map(message => message.text)).toEqual(['Вопрос перед потерей связи', 'Полученный ответ']);
    expect(result.current.pending).toBeNull();
    expect(result.current.running).toBe(false);
  });
  it('reports expired auth even on a quiet refresh and stops future polling', async () => {
    let calls = 0;
    const fetcher = vi.fn(async () => ++calls === 1 ? response(stateFixture()) : ({ ok: false, status: 401, json: async () => ({ error: { code: 'unauthorized' } }) }) as Response);
    vi.stubGlobal('fetch', fetcher);
    const { result } = renderHook(() => usePastoral('signed'));
    await waitFor(() => expect(result.current.state).not.toBeNull());
    await act(async () => { await result.current.refresh(false); });
    expect(result.current.error?.code).toBe('unauthorized');
    await act(async () => { await result.current.refresh(false); });
    expect(fetcher).toHaveBeenCalledTimes(2);
  });
  it('retries unknown network delivery using the original UUID', async () => {
    let state = stateFixture();
    const requests: { request_id: string; text: string }[] = [];
    const fetcher = vi.fn(async (url: URL, options: RequestInit) => {
      if (url.pathname.endsWith('/state')) return response(state);
      const body = JSON.parse(options.body as string);
      requests.push(body);
      if (requests.length === 1) throw new TypeError('connection interrupted');
      state = { ...state, remaining_answers: 9, history: [{ id: 'u1', role: 'user', text: body.text, sources: [] }, { id: 'a1', role: 'assistant', text: 'Проверенный ответ', sources: [] }] };
      return response({ request_id: body.request_id, status: 'done', reply: { text: 'Проверенный ответ', sources: [] } });
    });
    vi.stubGlobal('fetch', fetcher);
    const { result } = renderHook(() => usePastoral('signed'));
    await waitFor(() => expect(result.current.state).not.toBeNull());
    act(() => result.current.send('Мой вопрос'));
    await waitFor(() => expect(result.current.error?.code).toBe('network'));
    act(() => result.current.retry());
    await waitFor(() => expect(result.current.messages.at(-1)?.text).toBe('Проверенный ответ'));
    expect(requests).toHaveLength(2);
    expect(requests[0].request_id).toBe(requests[1].request_id);
    expect(result.current.state?.remaining_answers).toBe(9);
  });

  it('ignores a late completed answer after stop advances epoch', async () => {
    let state = stateFixture();
    let complete: ((value: Response) => void) | undefined;
    vi.stubGlobal('fetch', vi.fn(async (url: URL, options: RequestInit) => {
      if (url.pathname.endsWith('/state')) return response(state);
      if (url.pathname.endsWith('/stop')) { state = stateFixture({ epoch: 2 }); return response(state); }
      if (options.method === 'POST') return response({ request_id: 'test', status: 'running' });
      return new Promise<Response>(resolve => { complete = resolve; });
    }));
    const { result } = renderHook(() => usePastoral('signed'));
    await waitFor(() => expect(result.current.state).not.toBeNull());
    act(() => result.current.send('Медленный вопрос'));
    await waitFor(() => expect(complete).toBeDefined(), { timeout: 2500 });
    await act(async () => { await result.current.control('stop'); });
    await act(async () => complete!(response({ request_id: 'test', status: 'done', reply: { text: 'Поздний чужой эпохе ответ', sources: [] } } satisfies Job)));
    expect(result.current.state?.epoch).toBe(2);
    expect(result.current.messages).toEqual([]);
    expect(result.current.pending).toBeNull();
  });

  it('clears private note, draft and messages when the session expires while idle', async () => {
    vi.useFakeTimers();
    const started = Date.now();
    const initial = stateFixture({ mode: 'confession', history: [{ id: 'private', role: 'user', text: 'Временный личный текст', sources: [] }], temporary: { active: true, expired: false, idle_seconds: 1800, max_seconds: 7200, remaining_seconds: 0.1 } });
    vi.stubGlobal('fetch', vi.fn(async () => response(Date.now() - started >= 100 ? { ...initial, history: [], temporary: { ...initial.temporary, active: false, expired: true } } : initial)));
    const { result } = renderHook(() => usePastoral('signed'));
    await act(async () => { await Promise.resolve(); });
    act(() => { result.current.setNote('Личная заметка'); result.current.setDraft('Черновик'); });
    await act(async () => { await vi.advanceTimersByTimeAsync(101); });
    expect(result.current.note).toBe('');
    expect(result.current.draft).toBe('');
    expect(result.current.messages).toEqual([]);
    expect(result.current.state?.mode).toBe('confession');
    expect(result.current.state?.temporary.expired).toBe(true);
  });

  it('reconciles accepted optimistic text with a stored message without duplicating it', async () => {
    let state = stateFixture();
    vi.stubGlobal('fetch', vi.fn(async (url: URL, options: RequestInit) => {
      if (url.pathname.endsWith('/state')) return response(state);
      if (options.method === 'POST') {
        const body = JSON.parse(options.body as string);
        state = { ...state, history: [{ id: 'db-42', role: 'user', text: body.text, sources: [] }] };
        return response({ request_id: body.request_id, status: 'running' });
      }
      return new Promise<Response>(() => {});
    }));
    const { result } = renderHook(() => usePastoral('signed'));
    await waitFor(() => expect(result.current.state).not.toBeNull());
    act(() => result.current.send('Один вопрос'));
    await waitFor(() => expect(result.current.pending?.accepted).toBe(true));
    await act(async () => { await result.current.refresh(); });
    expect(result.current.messages.filter(message => message.text === 'Один вопрос')).toHaveLength(1);
  });
});
