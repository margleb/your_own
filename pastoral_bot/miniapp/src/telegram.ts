import { useEffect } from 'react';

export function useTelegramBack(visible: boolean, onBack: () => void) {
  useEffect(() => {
    const app = window.Telegram?.WebApp;
    if (!app) return;
    app.ready();
    app.expand();
    try {
      if (app.isVersionAtLeast?.('6.1')) {
        app.setHeaderColor?.('#f7f4ed');
        app.setBackgroundColor?.('#f7f4ed');
      }
      if (app.isVersionAtLeast?.('7.10')) app.setBottomBarColor?.('#f7f4ed');
    } catch { /* Older clients can keep their native colors. */ }
    const safeArea = () => {
      const supported = app.isVersionAtLeast?.('8.0') ?? false;
      const top = supported ? (app.safeAreaInset?.top ?? 0) + (app.contentSafeAreaInset?.top ?? 0) : 0;
      const bottom = supported ? Math.max(app.safeAreaInset?.bottom ?? 0, app.contentSafeAreaInset?.bottom ?? 0) : 0;
      document.documentElement.style.setProperty('--telegram-safe-top', `${top}px`);
      document.documentElement.style.setProperty('--telegram-safe-bottom', `${bottom}px`);
    };
    safeArea();
    app.onEvent?.('safeAreaChanged', safeArea);
    app.onEvent?.('contentSafeAreaChanged', safeArea);
    return () => {
      app.offEvent?.('safeAreaChanged', safeArea);
      app.offEvent?.('contentSafeAreaChanged', safeArea);
    };
  }, []);
  useEffect(() => {
    const app = window.Telegram?.WebApp;
    if (!app) return;
    const back = app.BackButton;
    if (back && (app.isVersionAtLeast?.('6.1') ?? true)) {
      if (visible) back.show(); else back.hide();
      back.onClick(onBack);
    }
    return () => {
      back?.offClick(onBack);
    };
  }, [visible, onBack]);
}
