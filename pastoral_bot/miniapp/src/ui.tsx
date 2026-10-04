import type { ReactNode } from 'react';
import type { Screen } from './types';
type IconName =
  | "arrow"
  | "book"
  | "chat"
  | "check"
  | "chevron"
  | "clock"
  | "copy"
  | "feather"
  | "home"
  | "info"
  | "menu"
  | "pause"
  | "send"
  | "settings"
  | "shield"
  | "spark"
  | "trash"
  | "x";

export function Icon({ name, size = 20 }: { name: IconName; size?: number }) {
  const paths: Record<IconName, ReactNode> = {
    arrow: <path d="m15 18-6-6 6-6" />,
    book: (
      <>
        <path d="M4 5.5A2.5 2.5 0 0 1 6.5 3H11a2 2 0 0 1 2 2v15a2.5 2.5 0 0 0-2.5-2.5H4Z" />
        <path d="M20 5.5A2.5 2.5 0 0 0 17.5 3H13v17a2.5 2.5 0 0 1 2.5-2.5H20Z" />
      </>
    ),
    chat: (
      <>
        <path d="M21 15a4 4 0 0 1-4 4H8l-5 3V7a4 4 0 0 1 4-4h10a4 4 0 0 1 4 4Z" />
        <path d="M8 10h.01M12 10h.01M16 10h.01" />
      </>
    ),
    check: <path d="m5 12 4 4L19 6" />,
    chevron: <path d="m9 18 6-6-6-6" />,
    clock: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 7v5l3 2" />
      </>
    ),
    copy: (
      <>
        <rect x="8" y="8" width="11" height="11" rx="2" />
        <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
      </>
    ),
    feather: (
      <>
        <path d="M20.2 4.8c-3-3-8.5-1-11.7 2.2C6 9.5 5.2 12.4 5 15l-2 4 4-2c2.6-.2 5.5-1 8-3.5 3.2-3.2 5.2-8.7 2.2-11.7" />
        <path d="M6 16 15 7M10 12h5M8 14V9" />
      </>
    ),
    home: (
      <>
        <path d="m3 11 9-8 9 8" />
        <path d="M5 10v10h14V10M9 20v-6h6v6" />
      </>
    ),
    info: (
      <>
        <circle cx="12" cy="12" r="9" />
        <path d="M12 11v5M12 8h.01" />
      </>
    ),
    menu: (
      <>
        <circle cx="5" cy="12" r=".7" fill="currentColor" stroke="none" />
        <circle cx="12" cy="12" r=".7" fill="currentColor" stroke="none" />
        <circle cx="19" cy="12" r=".7" fill="currentColor" stroke="none" />
      </>
    ),
    pause: <rect x="6" y="6" width="12" height="12" rx="2" />,
    send: (
      <>
        <path d="m21 3-7.5 18-4.2-8.3L1 8.5Z" />
        <path d="M9.3 12.7 21 3" />
      </>
    ),
    settings: (
      <>
        <circle cx="12" cy="12" r="3" />
        <path d="M19.4 15a1.7 1.7 0 0 0 .3 1.9l.1.1-2.8 2.8-.1-.1a1.7 1.7 0 0 0-1.9-.3 1.7 1.7 0 0 0-1 1.6v.2h-4V21a1.7 1.7 0 0 0-1-1.6 1.7 1.7 0 0 0-1.9.3l-.1.1L4.2 17l.1-.1a1.7 1.7 0 0 0 .3-1.9A1.7 1.7 0 0 0 3 14H2.8v-4H3a1.7 1.7 0 0 0 1.6-1 1.7 1.7 0 0 0-.3-1.9L4.2 7 7 4.2l.1.1a1.7 1.7 0 0 0 1.9.3A1.7 1.7 0 0 0 10 3V2.8h4V3a1.7 1.7 0 0 0 1 1.6 1.7 1.7 0 0 0 1.9-.3l.1-.1L19.8 7l-.1.1a1.7 1.7 0 0 0-.3 1.9 1.7 1.7 0 0 0 1.6 1h.2v4H21a1.7 1.7 0 0 0-1.6 1Z" />
      </>
    ),
    shield: (
      <>
        <path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10Z" />
        <path d="m9 12 2 2 4-4" />
      </>
    ),
    spark: (
      <>
        <path d="M12 3c.6 4.3 2.7 6.4 7 7-4.3.6-6.4 2.7-7 7-.6-4.3-2.7-6.4-7-7 4.3-.6 6.4-2.7 7-7Z" />
        <path d="M19 17c.2 1.4.9 2.1 2.3 2.3-1.4.2-2.1.9-2.3 2.3-.2-1.4-.9-2.1-2.3-2.3 1.4-.2 2.1-.9 2.3-2.3Z" />
      </>
    ),
    trash: (
      <>
        <path d="M4 7h16M9 7V4h6v3M7 7l1 14h8l1-14M10 11v6M14 11v6" />
      </>
    ),
    x: <path d="m6 6 12 12M18 6 6 18" />,
  };
  return (
    <svg
      aria-hidden="true"
      className="icon"
      fill="none"
      height={size}
      viewBox="0 0 24 24"
      width={size}
    >
      {paths[name]}
    </svg>
  );
}

export function Button({
  children,
  className = "",
  icon,
  onClick,
  type = "button",
  disabled = false,
}: {
  children: ReactNode;
  className?: string;
  icon?: IconName;
  onClick?: () => void;
  type?: "button" | "submit";
  disabled?: boolean;
}) {
  return (
    <button disabled={disabled} className={`button ${className}`} onClick={onClick} type={type}>
      {icon && <Icon name={icon} />}
      <span>{children}</span>
    </button>
  );
}

export function Header({
  action,
  eyebrow,
  onBack,
  title,
}: {
  action?: ReactNode;
  eyebrow?: string;
  onBack: () => void;
  title: string;
}) {
  return (
    <header className="topbar">
      <button aria-label="Назад" className="icon-button" onClick={onBack}>
        <Icon name="arrow" />
      </button>
      <div className="topbar-title">
        {eyebrow && <span>{eyebrow}</span>}
        <strong>{title}</strong>
      </div>
      <div className="topbar-action">{action}</div>
    </header>
  );
}

export function BottomNav({
  active,
  onNavigate,
}: {
  active: "home" | "chats" | "settings";
  onNavigate: (screen: Screen) => void;
}) {
  return (
    <nav aria-label="Основная навигация" className="bottom-nav">
      <button
        aria-current={active === "home" ? "page" : undefined}
        className={active === "home" ? "active" : ""}
        onClick={() => onNavigate("home")}
      >
        <Icon name="home" />
        <span>Главная</span>
      </button>
      <button
        aria-current={active === "chats" ? "page" : undefined}
        className={active === "chats" ? "active" : ""}
        onClick={() => onNavigate("chat")}
      >
        <Icon name="chat" />
        <span>Беседы</span>
      </button>
      <button
        aria-current={active === "settings" ? "page" : undefined}
        className={active === "settings" ? "active" : ""}
        onClick={() => onNavigate("settings")}
      >
        <Icon name="settings" />
        <span>Настройки</span>
      </button>
    </nav>
  );
}

export function BookLight() {
  return (
    <div aria-hidden="true" className="book-light">
      <svg fill="none" viewBox="0 0 260 150">
        <path className="light-ray" d="m130 104 49-94M130 104l7-100M130 104 99-55" />
        <path className="book-page" d="M28 84c35-12 68-5 102 20v35c-33-22-66-29-102-17Z" />
        <path className="book-page" d="M232 84c-35-12-68-5-102 20v35c33-22 66-29 102-17Z" />
        <path className="book-line" d="M130 104v35M42 94c29-6 56 1 77 16M218 94c-29-6-56 1-77 16" />
      </svg>
    </div>
  );
}
