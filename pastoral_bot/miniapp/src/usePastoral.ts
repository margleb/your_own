import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { ApiError, PastoralApi } from './api';
import type { BotState, Job, Message, Mode } from './types';

interface Pending {
  id: string;
  text: string;
  epoch: number;
  note: boolean;
  accepted: boolean;
  baselineIds: string[];
  baselineHistory: string;
}
interface Completed {
  epoch: number;
  request: Pending;
  user: Message;
  assistant: Message;
}
const NOTE_REQUEST = 'Помоги составить краткую личную заметку для разговора со священником по нашей текущей беседе. Используй только то, что я уже рассказал, не добавляй поступков или выводов о моей виновности. Это редактируемый черновик от первого лица, без наставлений и цитат.';

function wait(signal: AbortSignal, milliseconds: number) {
  return new Promise<void>((resolve, reject) => {
    if (signal.aborted) { reject(new DOMException('Cancelled', 'AbortError')); return; }
    const abort = () => { clearTimeout(timer); reject(new DOMException('Cancelled', 'AbortError')); };
    const timer = setTimeout(() => { signal.removeEventListener('abort', abort); resolve(); }, milliseconds);
    signal.addEventListener('abort', abort, { once: true });
  });
}

export function usePastoral(initData: string) {
  const api = useMemo(() => new PastoralApi(initData), [initData]);
  const [state, setState] = useState<BotState | null>(null);
  const [error, setError] = useState<ApiError | null>(null);
  const [loading, setLoading] = useState(Boolean(initData));
  const [mutating, setMutating] = useState(false);
  const [pending, setPending] = useState<Pending | null>(null);
  const [running, setRunning] = useState(false);
  const [draft, setDraft] = useState('');
  const [note, setNote] = useState('');
  const [completed, setCompleted] = useState<Completed | null>(null);
  const completedRef = useRef<Completed | null>(null);
  const current = useRef<BotState | null>(null);
  const pendingRef = useRef<Pending | null>(null);
  const revision = useRef(0);
  const active = useRef(true);
  const jobAbort = useRef<AbortController | null>(null);
  const controlAbort = useRef<AbortController | null>(null);
  const changing = useRef(false);
  const fetches = useRef(new Set<AbortController>());
  const refreshSequence = useRef(0);
  const appliedSequence = useRef(0);
  const privateDeadline = useRef<number | null>(null);
  const authExpired = useRef(false);

  const clearPrivate = useCallback(() => {
    const latest = current.current;
    if (!latest || latest.mode !== 'confession') return;
    revision.current += 1;
    jobAbort.current?.abort();
    pendingRef.current = null;
    setPending(null);
    setRunning(false);
    setNote('');
    setDraft('');
    completedRef.current = null;
    setCompleted(null);
    const next = { ...latest, history: [], temporary: { ...latest.temporary, active: false, expired: true, remaining_seconds: 0 } };
    current.current = next;
    setState(next);
    privateDeadline.current = null;
  }, []);

  const cancel = useCallback(() => {
    revision.current += 1;
    jobAbort.current?.abort();
    jobAbort.current = null;
    pendingRef.current = null;
    setPending(null);
    setRunning(false);
    completedRef.current = null;
    setCompleted(null);
  }, []);
  const apply = useCallback((next: BotState) => {
    const previous = current.current;
    const changed = previous && (previous.epoch !== next.epoch || previous.mode !== next.mode || previous.conversation_id !== next.conversation_id);
    const expired = next.mode === 'confession' && next.temporary.expired;
    if (changed || expired) {
      cancel();
      setNote('');
      setDraft('');
    }
    const bridge = completedRef.current;
    if (bridge && JSON.stringify(next.history) !== bridge.request.baselineHistory && next.history.some(message => message.role === 'assistant' && message.text === bridge.assistant.text && !bridge.request.baselineIds.includes(message.id))) {
      completedRef.current = null;
      setCompleted(null);
    }
    current.current = next;
    privateDeadline.current = next.mode === 'confession' && next.temporary.active
      ? Date.now() + Math.max(0, next.temporary.remaining_seconds) * 1000 : null;
    setState(next);
  }, [cancel]);

  const refresh = useCallback(async (reportError = true) => {
    if (!initData || changing.current || authExpired.current) return;
    const controller = new AbortController();
    fetches.current.add(controller);
    const version = revision.current;
    const sequence = ++refreshSequence.current;
    try {
      const next = await api.state(controller.signal);
      if (!active.current || controller.signal.aborted || changing.current || version !== revision.current || sequence < appliedSequence.current) return;
      appliedSequence.current = sequence;
      apply(next);
      if (reportError) setError(null);
    } catch (failure) {
      if (!controller.signal.aborted && active.current && version === revision.current) {
        const problem = asError(failure);
        if (problem.status === 401) {
          authExpired.current = true;
          cancel();
          clearPrivate();
          setError(problem);
        } else if (reportError) setError(problem);
      }
    } finally {
      fetches.current.delete(controller);
      if (active.current) setLoading(false);
    }
  }, [api, apply, initData, cancel, clearPrivate]);

  useEffect(() => {
    active.current = true;
    void refresh();
    const timer = setInterval(() => { void refresh(false); }, 10000);
    const foreground = () => {
      if (privateDeadline.current !== null && Date.now() >= privateDeadline.current) clearPrivate();
      if (document.visibilityState === 'visible') void refresh(false);
    };
    const background = () => {
      if (privateDeadline.current !== null && Date.now() >= privateDeadline.current) clearPrivate();
    };
    document.addEventListener('visibilitychange', foreground);
    window.addEventListener('pageshow', foreground);
    window.addEventListener('pagehide', background);
    return () => {
      active.current = false;
      revision.current += 1;
      clearInterval(timer);
      document.removeEventListener('visibilitychange', foreground);
      window.removeEventListener('pageshow', foreground);
      window.removeEventListener('pagehide', background);
      jobAbort.current?.abort();
      controlAbort.current?.abort();
      fetches.current.forEach(controller => controller.abort());
    };
  }, [refresh, clearPrivate]);

  // Hide expired private material even if connectivity disappears before the next GET.
  useEffect(() => {
    if (!state?.temporary.active || state.mode !== 'confession') return;
    const timer = setTimeout(() => {
      const latest = current.current;
      if (latest?.mode === 'confession' && latest.temporary.active) {
        clearPrivate();
        void refresh(false);
      }
    }, Math.max(0, state.temporary.remaining_seconds) * 1000);
    return () => clearTimeout(timer);
  }, [state, clearPrivate, refresh]);

  const control = useCallback(async (action: 'new' | 'stop' | 'delete-history' | Mode) => {
    if (!current.current || changing.current || authExpired.current) return false;
    changing.current = true;
    setMutating(true);
    setError(null);
    cancel();
    fetches.current.forEach(controller => controller.abort());
    const controller = new AbortController();
    controlAbort.current = controller;
    const before = current.current;
    try {
      const next = action === 'talk' || action === 'faith' || action === 'confession'
        ? await api.mode(action, before.epoch, controller.signal)
        : await api.control(action, before.epoch, controller.signal);
      if (!active.current || controller.signal.aborted) return false;
      apply(next);
      setDraft('');
      setNote('');
      return true;
    } catch (failure) {
      if (!controller.signal.aborted && active.current) {
        const problem = asError(failure);
        if (problem.status === 401) { authExpired.current = true; clearPrivate(); }
        setError(problem);
      }
      return false;
    } finally {
      changing.current = false;
      if (active.current) {
        setMutating(false);
        void refresh(false);
      }
    }
  }, [api, apply, cancel, refresh, clearPrivate]);

  const execute = useCallback(async (request: Pending) => {
    const controller = new AbortController();
    jobAbort.current?.abort();
    jobAbort.current = controller;
    const version = revision.current;
    const valid = () => active.current && !controller.signal.aborted && version === revision.current && current.current?.epoch === request.epoch;
    setError(null);
    setRunning(true);
    const finish = async (job: Job) => {
      if (!valid()) return;
      if (job.status === 'done' && job.reply) {
        // The backend remains authoritative for conversation history.
        if (request.note) setNote(job.reply.text);
        const bridge: Completed = {
          epoch: request.epoch,
          request,
          user: { id: `${request.id}-user`, role: 'user', text: request.text, sources: [] },
          assistant: { id: `${request.id}-assistant`, role: 'assistant', text: job.reply.text, sources: job.reply.sources },
        };
        completedRef.current = bridge;
        setCompleted(bridge);
        pendingRef.current = null;
        setPending(null);
        setRunning(false);
        await refresh(false);
      } else if (job.status === 'expired' || job.status === 'cancelled') {
        cancel();
        await refresh(false);
        if (active.current) setError(new ApiError(job.status, job.status === 'expired' ? 'Сессия или запрос завершены. Начните новую тему.' : 'Ответ остановлен.'));
      } else {
        pendingRef.current = null;
        setPending(null);
        setRunning(false);
        setError(new ApiError(job.error?.code ?? 'invalid_reply', job.error?.message ?? 'Не удалось подготовить ответ. Попробуйте позже.'));
        await refresh(false);
      }
    };
    try {
      let job = await api.send(request.id, request.text, request.epoch, controller.signal);
      if (!valid()) return;
      const accepted = { ...request, accepted: true };
      pendingRef.current = accepted;
      setPending(accepted);
      setDraft('');
      void refresh(false);
      while (job.status === 'pending' || job.status === 'running') {
        await wait(controller.signal, 1000);
        if (!valid()) return;
        job = await api.job(request.id, controller.signal);
      }
      await finish(job);
    } catch (failure) {
      if (!valid()) return;
      const problem = asError(failure);
      setError(problem);
      setRunning(false);
      if (problem.status === 401) { authExpired.current = true; cancel(); clearPrivate(); }
      if (problem.code !== 'network' && problem.code !== 'bad_response') {
        pendingRef.current = null;
        setPending(null);
        await refresh(false);
      }
      // Unknown delivery/network result: retain the same UUID for a safe retry.
    }
  }, [api, cancel, refresh, clearPrivate]);

  const send = useCallback((text: string, asNote = false) => {
    const context = current.current;
    if (!context || pendingRef.current || changing.current || authExpired.current || !text.trim() || context.remaining_answers <= 0 || text.length > context.max_message_chars || (context.mode === 'confession' && context.temporary.expired)) return;
    const request: Pending = { id: crypto.randomUUID(), text: text.trim(), epoch: context.epoch, note: asNote, accepted: false, baselineIds: context.history.map(message => message.id), baselineHistory: JSON.stringify(context.history) };
    pendingRef.current = request;
    setPending(request);
    void execute(request);
  }, [execute]);
  const retry = useCallback(() => {
    if (pendingRef.current && !running && !changing.current) void execute(pendingRef.current);
    else void refresh();
  }, [execute, refresh, running]);
  const generateNote = useCallback(() => send(NOTE_REQUEST, true), [send]);
  const messages: Message[] = state ? [...state.history] : [];
  if (pending?.accepted && !messages.some(message => message.role === 'user' && message.text === pending.text && !pending.baselineIds.includes(message.id))) {
    messages.push({ id: pending.id, role: 'user', text: pending.text, sources: [] });
  }
  if (completed?.epoch === state?.epoch && completed) {
    if (!messages.some(message => message.role === 'user' && message.text === completed.user.text && !completed.request.baselineIds.includes(message.id))) messages.push(completed.user);
    if (!messages.some(message => message.role === 'assistant' && message.text === completed.assistant.text && !completed.request.baselineIds.includes(message.id))) messages.push(completed.assistant);
  }
  return { state, messages, error, loading, mutating, pending, running, draft, setDraft, note, setNote, control, send, retry, refresh, generateNote };
}

function asError(failure: unknown): ApiError {
  return failure instanceof ApiError ? failure : new ApiError('unavailable', 'Сервис временно недоступен. Попробуйте позже.');
}
