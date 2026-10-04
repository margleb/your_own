export type Mode = 'talk' | 'faith' | 'confession';
export type Screen = 'home' | 'chat' | 'faith' | 'confession-intro' | 'temporary' | 'note' | 'settings';
export interface Source {
  source_id: string;
  title: string;
  edition: string;
  locator: string;
  url: string;
  text: string;
}
export interface Message {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  sources: Source[];
}
export interface BotState {
  mode: Mode;
  epoch: number;
  conversation_id: string;
  history: Message[];
  remaining_answers: number;
  daily_answer_limit: number;
  max_message_chars: number;
  temporary: {
    active: boolean;
    expired: boolean;
    idle_seconds: number;
    max_seconds: number;
    remaining_seconds: number;
  };
}
export interface Job {
  request_id: string;
  status: 'pending' | 'running' | 'done' | 'cancelled' | 'error' | 'expired';
  epoch?: number;
  reply?: { text: string; sources: Source[]; referral?: string | null };
  error?: { code: string; message: string };
}
export interface TelegramWebApp {
  initData: string;
  version?: string;
  ready(): void;
  expand(): void;
  setHeaderColor?(color: string): void;
  setBackgroundColor?(color: string): void;
  setBottomBarColor?(color: string): void;
  isVersionAtLeast?(version: string): boolean;
  BackButton?: { show(): void; hide(): void; onClick(callback: () => void): void; offClick(callback: () => void): void };
  safeAreaInset?: { top: number; bottom: number; left: number; right: number };
  contentSafeAreaInset?: { top: number; bottom: number; left: number; right: number };
  onEvent?(name: string, callback: () => void): void;
  offEvent?(name: string, callback: () => void): void;
}
declare global {
  interface Window { Telegram?: { WebApp?: TelegramWebApp } }
}
