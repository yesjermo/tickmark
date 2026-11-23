import { memo, useEffect, useRef } from "react";

/** TradingView's official embeddable widgets (free, with TradingView's attribution link). Prices come straight from TradingView,
 *  so they're for viewing: Tickmark's rules keep using their own completed daily bars. */
function useWidget(kind: string, config: Record<string, unknown>, deps: unknown[]) {
  const box = useRef<HTMLDivElement>(null);
  const cur = useRef<{ key: string; host: HTMLDivElement } | null>(null);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    // Build once per config. TradingView's loader reads its own <script>'s parent while it loads, so a host is only replaced when
    // the symbol or theme changes, never torn down mid-load (React's dev double-mount would otherwise do exactly that).
    const key = JSON.stringify(config);
    if (cur.current?.key === key && cur.current.host.isConnected) return;
    cur.current?.host.remove();
    const host = document.createElement("div");
    host.className = "tv-host";
    host.innerHTML = '<div class="tradingview-widget-container__widget" style="height:100%;width:100%"></div>';
    el.appendChild(host);
    const s = document.createElement("script");
    s.src = `https://s3.tradingview.com/external-embedding/embed-widget-${kind}.js`;
    s.async = true;
    s.type = "text/javascript";
    s.innerHTML = JSON.stringify(config);
    host.appendChild(s);
    cur.current = { key, host };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);
  return box;
}

const theme = () => (document.documentElement.dataset.theme === "dark" ? "dark" : "light");

const TV_RANGE: Record<string, string | undefined> = { "1M": "1M", "3M": "3M", "6M": "6M", "1Y": "12M", "2Y": undefined };

export const TVChart = memo(function TVChart({ symbol, interval = "D", range }: { symbol: string; interval?: string; range?: string }) {
  const t = theme();
  const daily = ["D", "W", "M"].includes(interval);
  const box = useWidget("advanced-chart", {
    symbol, interval, ...(daily && range && TV_RANGE[range] ? { range: TV_RANGE[range] } : {}), timezone: "America/New_York", theme: t, style: "1", locale: "en", autosize: true,
    backgroundColor: t === "dark" ? "#171b1e" : "#f8f7f4", gridColor: t === "dark" ? "#1e2327" : "#ebe9e4",
    hide_top_toolbar: false, hide_legend: false, allow_symbol_change: false, save_image: false, calendar: false,
    studies: ["STD;EMA"], support_host: "https://www.tradingview.com",
  }, [symbol, t, interval, range]);
  return <div className="tradingview-widget-container tv-chart" ref={box} />;
});

export const TVTape = memo(function TVTape({ symbols }: { symbols: string[] }) {
  const t = theme();
  const key = symbols.join(",");
  const box = useWidget("ticker-tape", {
    // Index CFDs (FOREXCOM:SPXUSD…) no longer load in free widgets; the SPY / QQQ ETFs track the same markets.
    symbols: [{ proName: "AMEX:SPY", title: "S&P 500 (SPY)" }, { proName: "NASDAQ:QQQ", title: "Nasdaq 100 (QQQ)" },
      ...symbols.map((s) => ({ proName: s, title: s }))],
    showSymbolLogo: false, isTransparent: true, displayMode: "compact", colorTheme: t, locale: "en",
  }, [key, t]);
  return <div className="tradingview-widget-container tv-tape" ref={box} aria-label="Live prices from TradingView" />;
});

/** Your own TradingView, in one reusable window beside the desk. TradingView has no sign-in for other apps, so you sign in on
 *  TradingView's own page; after that this window shows your saved layouts, indicators and drawings. */
const TV_WINDOW = "tickmark-tradingview";
const winFeatures = () => {
  const w = Math.min(1360, screen.availWidth - 40), h = Math.min(900, screen.availHeight - 40);
  return `popup,width=${w},height=${h},left=${Math.max(0, screen.availWidth - w)},top=20`;
};
export function openTradingViewSignIn() {
  window.open("https://www.tradingview.com/accounts/signin/", TV_WINDOW, winFeatures())?.focus();
}
const TV_INTERVAL: Record<string, string> = { "5": "5", "15": "15", "60": "60", D: "D", W: "W", M: "M" };
export function openMyTradingView(symbol: string, interval = "D", layoutUrl?: string | null) {
  const base = layoutUrl && /^https:\/\/www\.tradingview\.com\/chart\/[A-Za-z0-9]+\/$/.test(layoutUrl) ? layoutUrl : "https://www.tradingview.com/chart/";
  const url = `${base}?symbol=${encodeURIComponent(symbol)}&interval=${TV_INTERVAL[interval] ?? "D"}`;
  window.open(url, TV_WINDOW, winFeatures())?.focus();
}
