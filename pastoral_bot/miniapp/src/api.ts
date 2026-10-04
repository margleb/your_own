import type { BotState, Job, Mode } from './types';

export class ApiError extends Error {
  constructor(public code: string, message: string, public status = 0) {
    super(message);
    this.name = 'ApiError';
  }
}
export class PastoralApi {
  // Telegram's signed launch data stays in this instance's memory only.
  constructor(private readonly initData: string) {}
  private async request<T>(path: string, body?: unknown, signal?: AbortSignal): Promise<T> {
    if (!this.initData) throw new ApiError('unauthorized', 'Откройте приложение через Telegram.', 401);
    let response: Response;
    try {
      response = await fetch(new URL(`api/${path}`, document.baseURI), {
        method: body === undefined ? 'GET' : 'POST',
        headers: {
          Authorization: `tma ${this.initData}`,
          ...(body === undefined ? {} : { 'Content-Type': 'application/json' }),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        credentials: 'omit',
        cache: 'no-store',
        signal,
      });
    } catch (error) {
      if (signal?.aborted) throw error;
      throw new ApiError('network', 'Не удалось связаться с сервисом. Проверьте соединение и повторите запрос.');
    }
    let payload: unknown;
    try { payload = await response.json(); }
    catch { throw new ApiError('bad_response', 'Сервис вернул непонятный ответ. Попробуйте позже.', response.status); }
    if (!response.ok) {
      if (response.status === 401) throw new ApiError('unauthorized', 'Срок доступа истёк. Откройте приложение заново из бота.', 401);
      const details = (payload as { error?: { code?: string; message?: string } }).error;
      throw new ApiError(details?.code ?? 'server_error', details?.message ?? 'Сервис временно недоступен.', response.status);
    }
    return payload as T;
  }
  state(signal?: AbortSignal) { return this.request<BotState>('state', undefined, signal); }
  mode(mode: Mode, epoch: number, signal?: AbortSignal) { return this.request<BotState>('mode', { mode, expected_epoch: epoch }, signal); }
  control(action: 'new' | 'stop' | 'delete-history', epoch: number, signal?: AbortSignal) {
    return this.request<BotState>(action, { expected_epoch: epoch, ...(action === 'delete-history' ? { confirm: true } : {}) }, signal);
  }
  send(requestId: string, text: string, epoch: number, signal?: AbortSignal) {
    return this.request<Job>('messages', { request_id: requestId, text, expected_epoch: epoch }, signal);
  }
  job(requestId: string, signal?: AbortSignal) { return this.request<Job>(`messages/${encodeURIComponent(requestId)}`, undefined, signal); }
}

export function safeSourceUrl(value: string): string | null {
  try {
    const url = new URL(value);
    return url.protocol === 'https:' && !url.username && !url.password ? url.href : null;
  } catch { return null; }
}
