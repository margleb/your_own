import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import App, { SourceSheet } from '../App';
import type { BotState } from '../types';

describe('public and private UI', () => {
  it('shows the public homepage without mock chats or an auth bypass', () => {
    const fetcher = vi.fn();
    vi.stubGlobal('fetch', fetcher);
    render(<App />);
    expect(screen.getByRole('link', { name: 'Открыть в Telegram' })).toHaveAttribute('href', 'https://t.me/pastoralOrthBot');
    fireEvent.click(screen.getByRole('button', { name: /Поговорить О переживаниях/ }));
    expect(screen.queryByRole('textbox')).not.toBeInTheDocument();
    expect(fetcher).not.toHaveBeenCalled();
    expect(screen.queryByText(/Спасибо, что поделились/)).not.toBeInTheDocument();
  });
  it('renders source text as text and omits unsafe links', () => {
    render(<SourceSheet source={{ source_id: 'known', title: 'Евангелие', edition: 'Синодальный', locator: 'Мф. 6:12', url: 'javascript:alert(1)', text: '<img src=x onerror=alert(1)> Текст' }} onClose={vi.fn()} />);
    expect(screen.getByText('<img src=x onerror=alert(1)> Текст')).toBeInTheDocument();
    expect(document.querySelector('img')).toBeNull();
    expect(screen.queryByRole('link')).not.toBeInTheDocument();
  });
  it('preserves note edits within navigation and asks before leaving an active private mode', async () => {
    window.Telegram = { WebApp: { initData: 'signed', ready: vi.fn(), expand: vi.fn() } };
    const state: BotState = { mode: 'confession', epoch: 5, conversation_id: 'private-session', history: [], remaining_answers: 9, daily_answer_limit: 10, max_message_chars: 6000, temporary: { active: true, expired: false, idle_seconds: 1800, max_seconds: 7200, remaining_seconds: 1000 } };
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => state });
    vi.stubGlobal('fetch', fetcher);
    render(<App />);
    await waitFor(() => expect(screen.getByRole('button', { name: /Поговорить О переживаниях/ })).not.toBeDisabled());
    fireEvent.click(screen.getByRole('button', { name: /Подготовиться к исповеди/ }));
    fireEvent.click(screen.getByRole('button', { name: 'Продолжить подготовку' }));
    fireEvent.click(screen.getByRole('button', { name: 'Составить заметку' }));
    fireEvent.change(screen.getByLabelText('Текст заметки'), { target: { value: 'Моя личная заметка' } });
    fireEvent.click(screen.getByRole('button', { name: 'Вернуться к беседе' }));
    fireEvent.click(screen.getByRole('button', { name: 'Открыть заметку' }));
    expect(screen.getByLabelText('Текст заметки')).toHaveValue('Моя личная заметка');
    fireEvent.click(screen.getByRole('button', { name: 'Назад' }));
    fireEvent.click(screen.getByRole('button', { name: 'Назад' }));
    fireEvent.click(screen.getByRole('button', { name: /Узнать о вере Ответы/ }));
    expect(screen.getByRole('dialog')).toHaveTextContent('Переход в другой режим завершит временную беседу');
    expect(fetcher.mock.calls.every(([, options]) => options.method === 'GET')).toBe(true);
    fireEvent.click(screen.getByRole('button', { name: 'Отмена' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });
});
