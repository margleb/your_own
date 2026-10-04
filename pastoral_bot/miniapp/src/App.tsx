import { useCallback, useEffect, useId, useRef, useState, type ReactNode } from 'react';
import { safeSourceUrl } from './api';
import { useTelegramBack } from './telegram';
import { usePastoral } from './usePastoral';
import { BookLight, BottomNav, Button, Header, Icon } from './ui';
import type { Mode, Screen, Source } from './types';

const TELEGRAM_URL = 'https://t.me/pastoralOrthBot';
const modeScreen = (mode: Mode): Screen => mode === 'confession' ? 'temporary' : mode === 'faith' ? 'faith' : 'chat';
type Controller = ReturnType<typeof usePastoral>;
type ModalKind = 'delete' | 'exit' | 'storage' | null;

function TelegramLink({ reopen = false }: { reopen?: boolean }) {
  return <a className="button primary wide" href={TELEGRAM_URL} rel="noreferrer" target="_blank"><Icon name="chat" /><span>{reopen ? 'Открыть заново в Telegram' : 'Открыть в Telegram'}</span></a>;
}

export default function App() {
  const [initData] = useState(() => window.Telegram?.WebApp?.initData ?? '');
  const bot = usePastoral(initData);
  const [screen, setScreen] = useState<Screen>('home');
  const [source, setSource] = useState<Source | null>(null);
  const [modal, setModal] = useState<ModalKind>(null);
  const [nextMode, setNextMode] = useState<Mode | null>(null);
  const [toast, setToast] = useState('');
  const unauthorized = bot.error?.code === 'unauthorized' || bot.error?.status === 401;
  const connected = Boolean(initData) && !unauthorized;
  const ready = connected && Boolean(bot.state) && !bot.loading;
  const back = useCallback(() => {
    if (source) setSource(null);
    else if (modal) { setModal(null); setNextMode(null); }
    else setScreen(screen === 'note' ? 'temporary' : 'home');
  }, [source, modal, screen]);
  useTelegramBack(screen !== 'home' || Boolean(source || modal), back);
  useEffect(() => {
    if (!toast) return;
    const timer = setTimeout(() => setToast(''), 3500);
    return () => clearTimeout(timer);
  }, [toast]);
  useEffect(() => {
    if (screen === 'note' && (bot.state?.mode !== 'confession' || bot.state.temporary.expired)) setScreen('temporary');
    if (source && (bot.state?.mode === 'confession' && bot.state.temporary.expired)) setSource(null);
  }, [bot.state, screen, source]);

  const navigate = (next: Screen) => {
    if (next === 'chat' || next === 'faith') {
      if (!ready) { setScreen('home'); setToast('Беседа доступна в приложении Telegram.'); return; }
      if (next === 'chat') setScreen(modeScreen(bot.state!.mode));
      else void chooseMode('faith');
    } else setScreen(next);
    window.scrollTo({ top: 0, behavior: 'instant' });
  };
  const chooseMode = async (mode: Mode) => {
    if (!ready) { setToast('Откройте помощника в Telegram, чтобы начать беседу.'); return; }
    if (bot.state?.mode === 'confession' && bot.state.temporary.active && mode !== 'confession') {
      setNextMode(mode);
      setModal('exit');
      return;
    }
    if (bot.state?.mode !== mode && !await bot.control(mode)) return;
    setScreen(modeScreen(mode));
  };
  const startTemporary = async () => {
    if (!ready) return;
    if (bot.state?.mode !== 'confession') {
      if (!await bot.control('confession')) return;
    } else if (!bot.state.temporary.active) {
      if (!await bot.control('new')) return;
    }
    setScreen('temporary');
  };
  const noteScreen = () => {
    if (!bot.state?.temporary.active) return;
    setScreen('note');
  };
  const confirm = async (kind: 'delete' | 'exit') => {
    if (kind === 'exit' && nextMode) {
      if (!await bot.control(nextMode)) return;
      setScreen(modeScreen(nextMode));
      setNextMode(null);
      setModal(null);
      setSource(null);
      return;
    }
    if (!await bot.control(kind === 'delete' ? 'delete-history' : 'stop')) return;
    setSource(null);
    setModal(null);
    setScreen(kind === 'exit' ? 'home' : 'settings');
    setToast(kind === 'exit' ? 'Временная сессия завершена.' : 'Сохранённая история удалена.');
  };
  const copy = async () => {
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(bot.note);
      setToast('Заметка скопирована.');
    } catch {
      const field = document.querySelector<HTMLTextAreaElement>('[data-note-editor]');
      field?.focus();
      field?.select();
      try {
        if (document.execCommand('copy')) { setToast('Заметка скопирована.'); return; }
      } catch { /* The selected field remains available for manual copying. */ }
      setToast('Текст выделен. Скопируйте его через меню устройства.');
    }
  };

  return <div className="app-shell" aria-busy={bot.mutating}>
    {screen === 'home' && <Home connected={connected} loading={bot.loading} ready={ready} onMode={chooseMode} onPreparation={() => setScreen('confession-intro')} onNavigate={navigate} />}
    {(screen === 'chat' || screen === 'faith' || screen === 'temporary') && <Chat bot={bot} onBack={() => setScreen('home')} onSource={setSource} onStorage={() => setModal('storage')} onExit={() => setModal('exit')} onNote={noteScreen} />}
    {screen === 'confession-intro' && <ConfessionIntro bot={bot} ready={ready} onBack={() => setScreen('home')} onStart={startTemporary} />}
    {screen === 'note' && <div className="screen note-screen">
      <Header title="Личная заметка" onBack={() => setScreen('temporary')} />
      <main className="note-content">
        <div className="note-heading"><span className="note-icon"><Icon name="feather" /></span><div><p className="kicker">Черновик</p><h1>Главное для разговора</h1></div></div>
        <p className="note-lead">Можно написать самостоятельно или попросить помощника собрать главное из текущей беседы.</p>
        <label className="note-field"><span>Текст заметки</span><textarea aria-label="Текст заметки" data-note-editor value={bot.note} onChange={event => bot.setNote(event.target.value)} disabled={bot.running || bot.mutating} /><small>{bot.note.length} знаков</small></label>
        <div className="note-private"><Icon name="shield" size={18} /><span>Этот черновик остаётся в памяти приложения до завершения сессии. После копирования вы храните копию самостоятельно.</span></div>
        <Button className="primary wide" icon="copy" disabled={!bot.note.trim()} onClick={() => void copy()}>Скопировать</Button>
        <Button className="secondary wide" icon="feather" disabled={!bot.messages.length || bot.running || bot.mutating || Boolean(bot.pending) || !bot.state?.remaining_answers} onClick={bot.generateNote}>{bot.running ? 'Составляем заметку…' : bot.note ? 'Составить заново с ИИ' : 'Составить с ИИ'}</Button>
        <p className="fine-print">Составление с ИИ использует один ответ из суточного лимита.</p>
        <Button className="secondary wide" onClick={() => setScreen('temporary')}>Вернуться к беседе</Button>
        <ErrorNotice bot={bot} />
      </main>
    </div>}
    {screen === 'settings' && <Settings bot={bot} ready={ready} onDelete={() => setModal('delete')} onEnd={() => setModal('exit')} onNavigate={navigate} />}
    {(screen === 'home' || screen === 'confession-intro' || screen === 'settings') && <ErrorNotice bot={bot} />}
    {unauthorized && <div className="auth-notice" role="alert"><p>Срок доступа истёк. Откройте приложение заново из бота.</p><TelegramLink reopen /></div>}
    {source && <SourceSheet source={source} onClose={() => setSource(null)} />}
    {modal && <Dialog title={modal === 'delete' ? 'Удалить историю?' : modal === 'exit' ? 'Завершить временную сессию?' : 'Временное хранение'} onClose={() => { if (!bot.mutating) { setModal(null); setNextMode(null); } }}>
      <span className="modal-icon"><Icon name={modal === 'delete' ? 'trash' : modal === 'exit' ? 'clock' : 'shield'} /></span>
      <h2>{modal === 'delete' ? 'Удалить историю?' : modal === 'exit' ? 'Завершить временную сессию?' : 'Временное хранение'}</h2>
      {modal === 'delete' && <><p>Все сохранённые беседы и их поисковые фрагменты будут удалены. Незавершённый ответ отменится; временная заметка очистится. Удаление повторно применяется при восстановлении резервной копии.</p><Button className="danger wide" disabled={bot.mutating} onClick={() => void confirm('delete')}>{bot.mutating ? 'Удаляем…' : 'Удалить историю'}</Button></>}
      {modal === 'exit' && <><p>{nextMode ? 'Переход в другой режим завершит временную беседу. ' : ''}Текст беседы и заметка исчезнут из памяти сервиса и приложения. Скопируйте нужное перед завершением.</p><Button className="primary wide" disabled={bot.mutating} onClick={() => void confirm('exit')}>{bot.mutating ? 'Завершаем…' : nextMode ? 'Завершить и перейти' : 'Завершить'}</Button></>}
      {modal === 'storage' && <p>Текст подготовки существует в памяти сервиса до {duration(bot.state?.temporary.idle_seconds ?? 1800)} бездействия, не более {duration(bot.state?.temporary.max_seconds ?? 7200)} с начала беседы. При перезапуске сервиса он исчезает. Для ответа текст передаётся провайдеру модели с требованием Zero Data Retention. Мини-приложение не отправляет эту беседу сообщениями в Telegram. Переписка непосредственно с ботом в чате Telegram хранится по правилам Telegram.</p>}
      <ErrorNotice bot={bot} />
      <Button className="secondary wide" disabled={bot.mutating} onClick={() => { setModal(null); setNextMode(null); }}>{modal === 'storage' ? 'Понятно' : 'Отмена'}</Button>
    </Dialog>}
    {toast && <div className="toast" role="status">{toast}</div>}
  </div>;
}

function Home({ connected, loading, ready, onMode, onPreparation, onNavigate }: { connected: boolean; loading: boolean; ready: boolean; onMode: (mode: Mode) => void; onPreparation: () => void; onNavigate: (screen: Screen) => void }) {
  return <div className="screen home-screen"><main className="home-content">
    <div className="brand"><span className="brand-mark"><Icon name="book" size={22} /></span><div><strong>Православный помощник</strong><span>Независимый ИИ-проект</span></div></div>
    <BookLight />
    <section className="hero"><p className="kicker">Пространство для тихого разговора</p><h1>Можно начать<br />с простого разговора</h1><p>Без спешки и оценок. Выберите, что сейчас ближе.</p></section>
    <div className="action-list">
      {[{ icon: 'chat' as const, title: 'Поговорить', text: 'О переживаниях и духовной жизни', tone: 'sage', action: () => onMode('talk') }, { icon: 'book' as const, title: 'Узнать о вере', text: 'Ответы с проверяемыми источниками', tone: 'gold', action: () => onMode('faith') }, { icon: 'feather' as const, title: 'Подготовиться к исповеди', text: 'Личная заметка для разговора', tone: 'cream', action: onPreparation }].map((card, index) => <button className={`action-card ${card.tone}`} key={card.title} onClick={card.action} disabled={connected && !ready && index < 2}><span className="action-number">0{index + 1}</span><span className="action-icon"><Icon name={card.icon} /></span><span className="action-copy"><strong>{card.title}</strong><span>{card.text}</span></span><span className="action-arrow"><Icon name="chevron" /></span></button>)}
    </div>
    {!connected && <div className="public-intro"><p>Начните в Telegram: приложение узнает вас без отдельной регистрации.</p><TelegramLink /></div>}
    {loading && <p className="fine-print" role="status">Подключаем вашу беседу…</p>}
    <div className="ai-note"><Icon name="info" size={17} /><span>Я — ИИ, не священник</span></div>
  </main><BottomNav active="home" onNavigate={onNavigate} /></div>;
}

function Chat({ bot, onBack, onSource, onStorage, onExit, onNote }: { bot: Controller; onBack: () => void; onSource: (source: Source) => void; onStorage: () => void; onExit: () => void; onNote: () => void }) {
  const [menu, setMenu] = useState(false);
  const tail = useRef<HTMLDivElement>(null);
  const temporary = bot.state?.mode === 'confession';
  const faith = bot.state?.mode === 'faith';
  const expired = temporary && bot.state?.temporary.expired;
  const unauthorized = bot.error?.status === 401;
  const blocked = !bot.state || expired || bot.mutating || unauthorized || bot.state.remaining_answers <= 0;
  useEffect(() => { tail.current?.scrollIntoView({ block: 'end', behavior: 'smooth' }); }, [bot.messages.length, bot.running]);
  return <div className={`screen chat-screen ${temporary ? 'temporary-screen' : ''}`}>
    <Header onBack={onBack} title={temporary ? 'Временная сессия' : faith ? 'Узнать о вере' : 'Поговорить'} eyebrow={temporary ? 'Подготовка к разговору' : faith ? 'Ответы по источникам' : 'Спокойный разговор'} action={temporary ? <button aria-label="Сведения о хранении" className="icon-button" onClick={onStorage}><Icon name="info" /></button> : <div className="menu-anchor"><button aria-label="Меню беседы" aria-expanded={menu} className="icon-button" onClick={() => setMenu(!menu)}><Icon name="menu" /></button>{menu && <div className="popover"><button disabled={bot.mutating} onClick={() => { setMenu(false); void bot.control('new'); }}><Icon name="spark" size={18} />Новая тема</button></div>}</div>} />
    {temporary && <div className="temporary-banner"><Icon name="shield" size={18} /><span>Текст подготовки существует только в памяти сервиса</span></div>}
    <div className="usage-line"><span><span className="status-dot" />Осталось {bot.state?.remaining_answers ?? '—'} ответов сегодня</span><span>ИИ-помощник</span></div>
    <main className="messages" aria-busy={bot.running}>
      {!bot.messages.length && !expired && <p className="empty-conversation">{temporary ? 'Можно рассказать свободно о том, что вас тревожит. Интимные подробности здесь не нужны.' : faith ? 'Задайте вопрос своими словами. Ответ будет сопровождён найденными источниками.' : 'С чего вам хотелось бы начать? Здесь можно спокойно поговорить о том, что тревожит.'}</p>}
      {bot.messages.map(message => <div className="message-group" key={message.id}><div className={`message ${message.role}`}>{message.role === 'assistant' && <span className="message-mark"><Icon name={temporary ? 'feather' : 'book'} size={14} /></span>}<div className="bubble">{message.text}</div></div>{message.sources?.map(item => <button className="source-card" key={item.source_id} onClick={() => onSource(item)}><span className="source-icon"><Icon name="book" /></span><span><small>Источник ответа</small><strong>{item.title} · {item.locator}</strong><small>Открыть фрагмент</small></span><Icon name="chevron" /></button>)}</div>)}
      {faith && !bot.messages.length && !blocked && <div className="suggestions"><span>Возможно, вы ищете</span>{['Как начать молиться?', 'Что означает «Отче наш»?', 'Как подготовиться к причастию?'].map(text => <button className="suggestion" key={text} disabled={Boolean(bot.pending)} onClick={() => bot.send(text)}>{text}<Icon name="chevron" size={17} /></button>)}</div>}
      {bot.running && <div className="message assistant" role="status" aria-label="Помощник готовит ответ"><span className="message-mark"><Icon name="book" size={14} /></span><div className="bubble typing"><i /><i /><i /></div></div>}
      {expired && <div className="state-notice"><Icon name="clock" /><h2>Временная сессия завершена</h2><p>Текст и заметка очищены. Подготовка остаётся временным режимом: новая сессия не сохраняется в обычную историю.</p><Button className="primary wide" disabled={bot.mutating} onClick={() => void bot.control('new')}>Начать новую сессию</Button></div>}
      {bot.state?.remaining_answers === 0 && <p className="quota-notice" role="status">Ответы на сегодня закончились. Лимит обновится в полночь по московскому времени. Управление историей остаётся доступным.</p>}
      <ErrorNotice bot={bot} /><div ref={tail} />
    </main>
    {temporary && <div className="temporary-actions"><Button className="primary" icon="feather" disabled={expired || bot.mutating || bot.running} onClick={onNote}>{bot.note ? 'Открыть заметку' : 'Составить заметку'}</Button><Button className="quiet" disabled={!bot.state?.temporary.active || bot.mutating} onClick={onExit}>Завершить</Button></div>}
    <div className="composer-wrap"><form className="composer" onSubmit={event => { event.preventDefault(); bot.send(bot.draft); }}>
      <textarea aria-label="Сообщение" rows={1} value={bot.draft} maxLength={bot.state?.max_message_chars ?? 6000} disabled={blocked || Boolean(bot.pending)} onChange={event => bot.setDraft(event.target.value)} placeholder={expired ? 'Начните новую временную сессию' : 'Напишите сообщение…'} />
      {bot.pending ? <button aria-label="Остановить ответ" className="send-button stop" type="button" disabled={bot.mutating} onClick={() => void bot.control('stop')}><Icon name="pause" size={18} /></button> : <button aria-label="Отправить" className="send-button" disabled={blocked || !bot.draft.trim()} type="submit"><Icon name="send" size={19} /></button>}
    </form><p>{temporary ? 'Текст не сохраняется в базе сервиса' : 'Беседа сохраняется до удаления'}{bot.draft.length > 1000 ? ` · ${bot.draft.length}/${bot.state?.max_message_chars}` : ''}</p></div>
  </div>;
}

function ConfessionIntro({ bot, ready, onBack, onStart }: { bot: Controller; ready: boolean; onBack: () => void; onStart: () => void }) {
  return <div className="screen intro-screen"><Header onBack={onBack} title="Подготовка" /><main className="intro-content"><div className="intro-symbol"><Icon name="feather" size={32} /></div><p className="kicker">Личная заметка</p><h1>Соберите главное в спокойном темпе</h1><p className="intro-lead">Помогу осмыслить то, что вас тревожит, и сформулировать главное для разговора со священником.</p><div className="gentle-card"><Icon name="chat" /><div><strong>Расскажите свободно</strong><p>Без списка грехов и формальных вопросов. Здесь можно начать с того, что труднее всего выразить.</p></div></div><div className="memory-card"><div className="memory-heading"><span><Icon name="shield" /></span><div><small>Временная сессия</small><strong>Как существует текст</strong></div></div><ul><li><span>{duration(bot.state?.temporary.idle_seconds ?? 1800)}</span> бездействия</li><li><span>Не более {duration(bot.state?.temporary.max_seconds ?? 7200)}</span> с начала беседы</li><li><span>Исчезнет</span> после перезапуска сервиса</li></ul><p>Текст временно существует в памяти сервиса и приложения. Для ответа он передаётся провайдеру модели с требованием Zero Data Retention. Беседа мини-приложения не отправляется в чат Telegram.</p></div>{ready ? <Button className="primary wide" disabled={bot.mutating} onClick={onStart}>{bot.state?.mode === 'confession' && bot.state.temporary.active ? 'Продолжить подготовку' : 'Начать подготовку'}</Button> : <TelegramLink />}<p className="fine-print">Это подготовка к личному разговору. Помощник не совершает исповедь, не отпускает грехи и не назначает епитимью.</p></main></div>;
}

function Settings({ bot, ready, onDelete, onEnd, onNavigate }: { bot: Controller; ready: boolean; onDelete: () => void; onEnd: () => void; onNavigate: (screen: Screen) => void }) {
  return <div className="screen settings-screen"><main className="settings-content"><div className="settings-heading"><p className="kicker">Параметры проекта</p><h1>Настройки и приватность</h1></div><section className="settings-card about-card"><span className="settings-symbol"><Icon name="book" /></span><div><h2>О помощнике</h2><p>Независимый ИИ-проект для спокойного разговора и навигации по проверенным источникам о православной вере.</p><p>Он не заменяет священника, церковную общину или профессиональную помощь. Официальную принадлежность к РПЦ не заявляем.</p></div></section><section className="settings-card"><div className="setting-row static"><span className="row-icon"><Icon name="clock" /></span><div><strong>{bot.state ? `${bot.state.daily_answer_limit} ответов в сутки` : 'Бесплатное общение с суточным лимитом'}</strong><span>Обновление в полночь по Москве</span></div><span className="value">{bot.state?.remaining_answers ?? '—'}</span></div><div className="setting-row static"><span className="row-icon"><Icon name="shield" /></span><div><strong>О хранении</strong><span>Обычная история — до удаления</span></div></div><div className="telegram-note">Обычные беседы сохраняются в базе проекта. Резервные копии обычной истории зашифрованы и хранятся до 7 дней; удаления повторяются при восстановлении. Подготовка к исповеди и заметка существуют временно в памяти. Для ответа текст передаётся провайдеру модели с требованием Zero Data Retention. Мини-приложение не публикует эти беседы сообщениями в Telegram. Если вы пишете боту непосредственно в чате, та переписка хранится по правилам Telegram.</div></section><p className="section-label">Управление данными</p><section className="settings-card"><button className="setting-row" disabled={!ready || bot.mutating} onClick={onDelete}><span className="row-icon"><Icon name="trash" /></span><div><strong>Удалить обычную историю</strong><span>Все сохранённые беседы проекта</span></div><Icon name="chevron" size={18} /></button><button className="setting-row" disabled={!ready || !bot.state?.temporary.active || bot.mutating} onClick={onEnd}><span className="row-icon"><Icon name="x" /></span><div><strong>Завершить временную сессию</strong><span>{bot.state?.temporary.active ? 'Текст и заметка будут очищены' : 'Активной сессии нет'}</span></div><Icon name="chevron" size={18} /></button></section>{!ready && <TelegramLink />}<p className="version">Православный помощник · Независимый проект</p></main><BottomNav active="settings" onNavigate={onNavigate} /></div>;
}

function ErrorNotice({ bot }: { bot: Controller }) {
  if (!bot.error || bot.error.status === 401) return null;
  return <div className="error-notice" role="alert"><p>{bot.error.message}</p>{(bot.pending || !bot.state || bot.error.code === 'stale_epoch' || bot.error.code === 'network') && <Button className="secondary" disabled={bot.running || bot.mutating} onClick={bot.retry}>{bot.pending ? 'Повторить этот запрос' : 'Обновить состояние'}</Button>}</div>;
}

export function SourceSheet({ source, onClose }: { source: Source; onClose: () => void }) {
  const url = safeSourceUrl(source.url);
  return <Dialog title={source.title} sheet onClose={onClose}><div className="sheet-handle" /><div className="sheet-heading"><span><Icon name="book" /></span><div><small>Источник</small><h2>{source.title}</h2></div></div><p className="source-address">{source.locator}</p><p className="source-caption">{source.edition}</p><blockquote>{source.text}</blockquote><p className="source-caption">Фрагмент, использованный в ответе. Полный текст доступен на странице источника.</p>{url && <a className="button primary wide" href={url} target="_blank" rel="noopener noreferrer"><span>Открыть источник</span><Icon name="chevron" size={18} /></a>}</Dialog>;
}

function Dialog({ title, children, sheet = false, onClose }: { title: string; children: ReactNode; sheet?: boolean; onClose: () => void }) {
  const element = useRef<HTMLDivElement>(null);
  const titleId = useId();
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
    element.current?.querySelector<HTMLElement>('button, a, textarea')?.focus();
    return () => { document.body.style.overflow = previousOverflow; previous?.focus(); };
  }, []);
  return <div className={`overlay ${sheet ? 'sheet-overlay' : ''}`} onMouseDown={event => event.target === event.currentTarget && onClose()}><div ref={element} aria-modal="true" aria-labelledby={titleId} role="dialog" className={sheet ? 'sheet' : 'modal'} onKeyDown={event => {
    if (event.key === 'Escape') { event.stopPropagation(); onClose(); }
    if (event.key === 'Tab') {
      const focusable = [...(element.current?.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],textarea:not(:disabled)') ?? [])];
      const first = focusable[0], last = focusable[focusable.length - 1];
      if (event.shiftKey && document.activeElement === first) { event.preventDefault(); last?.focus(); }
      else if (!event.shiftKey && document.activeElement === last) { event.preventDefault(); first?.focus(); }
    }
  }}><span id={titleId} className="sr-only">{title}</span><button className="modal-close" aria-label="Закрыть" onClick={onClose}><Icon name="x" size={18} /></button>{children}</div></div>;
}

function duration(seconds: number) {
  return seconds >= 3600 && seconds % 3600 === 0 ? `${seconds / 3600} ч` : seconds >= 60 ? `${Math.ceil(seconds / 60)} мин` : `${Math.ceil(seconds)} сек`;
}
