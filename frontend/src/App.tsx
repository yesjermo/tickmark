import { useCallback, useEffect, useRef, useState } from "react";
import { api, Evaluation, Order, Ruleset, Status_ } from "./api";
import { TopBar } from "./components/TopBar";
import { Watchlist } from "./components/Watchlist";
import { PriceChart, Interval, Range } from "./components/PriceChart";
import { Evidence } from "./components/Evidence";
import { StrategyPanel } from "./components/StrategyPanel";
import { OrderTicket } from "./components/OrderTicket";
import { OrdersPanel } from "./components/OrdersPanel";
import { Journal } from "./components/Journal";
import { Forward } from "./components/Forward";
import { DayTrading } from "./components/DayTrading";
import { BrokerPanel } from "./components/BrokerPanel";
import { RiskPanel } from "./components/RiskPanel";
import { Documents } from "./components/Documents";
import { Copilot } from "./components/Copilot";
import { Crypto, CryptoForm } from "./components/Crypto";
import { Login, PortalUser } from "./components/Login";
import { AccountPanel } from "./components/AccountPanel";
import { Connections } from "./components/Connections";
import { CommandMenu, PaletteAction } from "./components/CommandMenu";
import { Toaster, toast as sonner } from "sonner";
import { AnimatePresence, MotionConfig, motion } from "motion/react";
import { FocusStrip, useComfort, useSmoothScroll } from "./components/Comfort";
import { useThemeKey } from "./components/theme";
import { AccountCard } from "./components/AccountCard";
import { Mark } from "./components/Mark";
import { TVChart, TVTape, openMyTradingView } from "./components/TradingView";
import { Portfolio } from "./components/Portfolio";
import { BarChart3, BookOpen, Bot, Briefcase, CandlestickChart, ClipboardList, FileText, Gauge, Bitcoin, ListChecks, Plug, ShieldCheck, Target, Timer, UserRound, Sun, Moon, PieChart, ExternalLink, LogIn } from "lucide-react";

export type Level = "beginner" | "analyst";
type CenterTab = "portfolio" | "evidence" | "journal" | "forward" | "documents" | "day" | "crypto" | "connections";
type SideTab = "copilot" | "strategy" | "order" | "orders" | "broker" | "risk" | "account";

export interface Screen {
  as_of: string | null; ruleset: Ruleset; results: Evaluation[]; errors: Record<string, string>; data_mode: string; needs_refresh: boolean;
  quality?: Record<string, { ok: boolean; issues: { kind: string; date: string | null; detail: string }[] }>; data_provider?: string | null;
}

export default function App() {
  const [auth, setAuth] = useState<{ portal: boolean; user: PortalUser | null; guest?: boolean; first_user?: boolean; open_signup?: boolean;
    admin_unclaimed?: boolean; github_login?: boolean; social?: string[] } | null>(null);
  const [authBox, setAuthBox] = useState<"in" | "up" | null>(null);
  // Like any website: visitors land on the desk as a guest (real prices, paper trades kept on this browser), and sign in when they want.
  const inflight = useRef<Promise<void> | null>(null);
  const wasUser = useRef(false);
  const load = () => inflight.current ?? (inflight.current = api.get<any>("/api/auth/status").then(async (a) => {
    if (a.user && a.user.role !== "guest") wasUser.current = true;
    if (a.portal && !a.user && !a.first_user && !wasUser.current && !new URLSearchParams(location.search).get("next")) {
      try { await api.post("/api/auth/guest"); a = await api.get<any>("/api/auth/status"); } catch { /* fall back to the sign-in page */ }
    }
    setAuth(a);
  }).catch(() => setAuth({ portal: false, user: null })).finally(() => { inflight.current = null; }));
  useEffect(() => {
    const q = new URLSearchParams(location.search);
    const msg = q.get("auth_error") ?? (q.get("signed_in") ? `Signed in with ${q.get("signed_in") === "google" ? "Google" : "GitHub"}.` : null);
    if (q.get("open") === "account") setTimeout(() => window.dispatchEvent(new Event("tickmark:open-account")), 800);
    if (msg) { setTimeout(() => (q.get("auth_error") ? sonner.error : sonner)(msg, { duration: 8000 }), 600); history.replaceState(null, "", location.pathname); }
    load();
    const out = () => { load(); };
    const up = () => setAuthBox("up");
    window.addEventListener("swingdesk:signup", up);
    window.addEventListener("swingdesk:signin", out);
    return () => { window.removeEventListener("swingdesk:signin", out); window.removeEventListener("swingdesk:signup", up); };
  }, []);
  if (!auth) return <div className="pad muted">Loading…</div>;
  if (auth.portal && !auth.user) return <Login firstUser={!!auth.first_user} openSignup={!!auth.open_signup} social={auth.social ?? []}
    onIn={(u) => setAuth({ ...auth, user: u, first_user: false })} />;
  return (
    <>
      <Desk adminUnclaimed={!!auth.admin_unclaimed} social={auth.social ?? []} user={auth.portal ? auth.user : null} key={`${auth.user?.id ?? "local"}:${auth.user?.role}`} guest={!!auth.guest} openAuth={setAuthBox} />
      <AnimatePresence>
        {authBox && <Login key="auth" firstUser={false} openSignup={!!auth.open_signup} social={auth.social ?? []} start={authBox} guest={!!auth.guest}
          onClose={() => setAuthBox(null)} onIn={() => { setAuthBox(null); load(); }} />}
      </AnimatePresence>
    </>
  );
}

function Desk({ user, guest = false, openAuth, adminUnclaimed = false, social = [] }: { user: PortalUser | null; guest?: boolean;
  openAuth: (m: "in" | "up") => void; adminUnclaimed?: boolean; social?: string[] }) {
  const [status, setStatus] = useState<Status_ | null>(null);
  const [screen, setScreen] = useState<Screen | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [journal, setJournal] = useState<any[]>([]);
  const [selected, setSelected] = useState("NVDA");
  const [level, setLevel] = useState<Level>("beginner");
  const [center, setCenter] = useState<CenterTab>("portfolio");
  const [side, setSide] = useState<SideTab>("strategy");
  const [tick, setTick] = useState(0);
  // Toasts through sonner: same notify(string) API the panels already use.
  const setToast = useCallback((m: string | null) => {
    if (!m) return;
    const bad = /couldn't|can't|failed|refused|blocked|error|not enabled|invalid|rejected|denied|expired/i.test(m);
    (bad ? sonner.error : sonner)(m, { duration: bad ? 9000 : 5000 });
  }, []);
  const [palette, setPalette] = useState(false);
  const comfort = useComfort();
  useEffect(() => {   // the installed app's "Account" shortcut (/?open=account)
    const open = () => { if (guest) openAuth("in"); else setSide("account"); };
    window.addEventListener("tickmark:open-account", open);
    return () => window.removeEventListener("tickmark:open-account", open);
  }, [guest, openAuth]);
  const [tvLayout, setTvLayout] = useState<string | null>(null);
  useEffect(() => {
    const get = () => api.get<{ layout_url: string | null }>("/api/tradingview").then((r) => setTvLayout(r.layout_url)).catch(() => {});
    get(); window.addEventListener("tickmark:tv-layout", get);
    return () => window.removeEventListener("tickmark:tv-layout", get);
  }, []);
  const themeKey = useThemeKey();
  const leftScroll = useSmoothScroll(comfort.smooth), centerScroll = useSmoothScroll(comfort.smooth), sideScroll = useSmoothScroll(comfort.smooth);
  const [chartSrc, setChartSrc] = useState<"rules" | "tv">(() => { try { return (localStorage.getItem("tickmark.chart") as "rules" | "tv") || "rules"; } catch { return "rules"; } });
  const pickChart = (c: "rules" | "tv") => { setChartSrc(c); try { localStorage.setItem("tickmark.chart", c); } catch { /* private mode */ } };
  const [tf, setTf] = useState<{ interval: Interval; range: Range }>(() => { try { return JSON.parse(localStorage.getItem("tickmark.tf") || "") } catch { return { interval: "D", range: "6M" }; } });
  const pickTf = (next: Partial<{ interval: Interval; range: Range }>) => {
    const v = { ...tf, ...next };
    setTf(v); try { localStorage.setItem("tickmark.tf", JSON.stringify(v)); } catch { /* private mode */ }
    if (next.interval && !["D", "W", "M"].includes(next.interval)) pickChart("tv");   // intraday candles come from TradingView
  };
  const [cryptoPrefill, setCryptoPrefill] = useState<CryptoForm | null>(null);
  const [prefill, setPrefill] = useState<{ symbol: string; overrides: Record<string, any>; n: number } | null>(null);

  const refresh = useCallback(() => setTick((t) => t + 1), []);

  useEffect(() => {
    // allSettled: one failing panel (e.g. a broker sync error) must not freeze the others on stale data.
    Promise.allSettled([
      api.get<Status_>("/api/status"),
      api.get<Screen>("/api/watchlist/screen"),
      api.get<Order[]>("/api/orders"),
      api.get<any[]>("/api/journal"),
    ]).then(([s, sc, o, j]) => {
      if (s.status === "fulfilled") setStatus(s.value);
      if (sc.status === "fulfilled") setScreen(sc.value);
      if (o.status === "fulfilled") setOrders(o.value);
      if (j.status === "fulfilled") setJournal(j.value);
      const failed = [["status", s], ["watchlist", sc], ["orders", o], ["journal", j]]
        .filter(([, r]) => (r as PromiseSettledResult<unknown>).status === "rejected")
        .map(([n, r]) => `${n}: ${((r as PromiseRejectedResult).reason as Error)?.message ?? "error"}`);
      if (failed.length) setToast(`Couldn't refresh ${failed.join("; ")}. Showing the last loaded data for those panels.`);
    });
  }, [tick]);

  // Server push: refresh when orders, alerts or data change (pushed broker fills, scheduled jobs).
  useEffect(() => {
    let es: EventSource | null = null;
    let timer: ReturnType<typeof setTimeout> | null = null;
    try {
      es = new EventSource("/api/stream");
      es.onmessage = () => {
        if (timer) clearTimeout(timer);
        timer = setTimeout(refresh, 300);   // coalesce bursts (fill + stop leg + alert)
      };
    } catch { /* EventSource unavailable: manual refresh still works */ }
    return () => { es?.close(); if (timer) clearTimeout(timer); };
  }, [refresh]);


  // A new desk loads real prices in the background on its first visit: check back until they arrive (about a minute at most).
  const [waits, setWaits] = useState(0);
  useEffect(() => {
    if (!screen?.needs_refresh || waits >= 12) return;
    const t = setTimeout(() => { setWaits((w) => w + 1); refresh(); }, 5000);
    return () => clearTimeout(t);
  }, [screen, waits, refresh]);

  // Keep the selection valid when the live watchlist changes.
  useEffect(() => {
    if (screen?.results.length && !screen.results.some((r) => r.symbol === selected)) setSelected(screen.results[0].symbol);
  }, [screen, selected]);

  const ev = screen?.results.find((r) => r.symbol === selected) ?? null;
  const live = status?.data_mode === "live";
  const CENTER: Record<CenterTab, string> = { portfolio: "Portfolio", evidence: "Rule evidence", journal: `Journal (${journal.length})`, forward: live ? "Live forward test" : "Forward test",
    documents: "Documents", day: "Day trading", crypto: "Crypto", connections: "Connections" };
  const CICON: Record<CenterTab, typeof Gauge> = { portfolio: PieChart, evidence: ListChecks, journal: BookOpen, forward: Timer, documents: FileText, day: CandlestickChart,
    crypto: Bitcoin, connections: Plug };
  const SHORT: Record<CenterTab, string> = { portfolio: "Portfolio", evidence: "Evidence", journal: "Journal", forward: "Forward", documents: "Docs", day: "Day", crypto: "Crypto", connections: "Connect" };
  const SIDE: Record<SideTab, string> = { copilot: "Copilot", strategy: "Strategy", order: "Ticket", orders: "Orders", broker: "Broker", risk: "Risk",
    account: user ? "Account" : "AI apps" };
  const SICON: Record<SideTab, typeof Gauge> = { copilot: Bot, strategy: Target, order: ClipboardList, orders: BarChart3, broker: Briefcase,
    risk: ShieldCheck, account: UserRound };
  const actions: PaletteAction[] = [
    ...(screen?.results ?? []).map((r) => ({ id: `sym-${r.symbol}`, group: "Symbols", label: r.symbol, hint: r.status.replace(/_/g, " ").toLowerCase(),
      icon: "symbol" as const, run: () => { setSelected(r.symbol); setCenter("evidence"); } })),
    ...(Object.keys(CENTER) as CenterTab[]).map((t) => ({ id: `c-${t}`, group: "Go to", label: CENTER[t], run: () => setCenter(t) })),
    ...(Object.keys(SIDE) as SideTab[]).map((t) => ({ id: `s-${t}`, group: "Go to", label: `${SIDE[t]} panel`, run: () => setSide(t) })),
    { id: "a-copilot", group: "Actions", label: "Ask the copilot", icon: "bot", keywords: "ai chat question", run: () => setSide("copilot") },
    { id: "a-connect", group: "Actions", label: "Connect a broker, exchange or AI", icon: "plug", keywords: "sign in login oauth key", run: () => setCenter("connections") },
    { id: "a-level", group: "Actions", label: level === "beginner" ? "Switch to Analyst view" : "Switch to Beginner view", run: () => setLevel(level === "beginner" ? "analyst" : "beginner") },
    { id: "a-refresh", group: "Actions", label: "Reload panels", icon: "refresh", run: refresh },
  ];

  // Focus mode shows one next step at a time.
  const best = screen?.results.find((r) => r.status === "QUALIFIES" || r.status === "QUALIFIES_WITH_WARNING");
  const nextStep = !screen ? { text: "Loading your watchlist…" }
    : !screen.ruleset.version ? { text: "Save your rules as a version before any paper trade.", action: "Open Strategy", go: () => setSide("strategy") }
    : status?.kill_switch?.on ? { text: "The kill switch is on. Nothing new will be sent until you turn it off.", action: "Open Risk", go: () => setSide("risk") }
    : best ? { text: `${best.symbol} qualifies. Read why, then decide on the ticket.`, action: `Review ${best.symbol}`, go: () => { setSelected(best.symbol); setCenter("evidence"); setSide("order"); } }
    : { text: "Nothing qualifies right now. Check again after the close; no trade is a fine outcome." };

  return (
    <MotionConfig reducedMotion={comfort.focus || comfort.motionLevel === "off" ? "always" : "user"}>
    <div className="app">
      <nav className="rail" aria-label="Sections">
        <div className="rail-mark"><Mark size={16} /></div>
        {(Object.keys(CENTER) as CenterTab[]).map((t) => {
          const Icon = CICON[t];
          return (
            <button key={t} className={`rail-btn ${center === t ? "on" : ""}`} onClick={() => setCenter(t)} aria-current={center === t ? "page" : undefined}
              aria-label={CENTER[t]} data-tip={CENTER[t]}>
              <Icon size={18} strokeWidth={1.8} aria-hidden /><span className="rail-label">{SHORT[t]}</span>
            </button>
          );
        })}
        <span className="rail-spacer" />
        {user && (
          <button className="rail-btn rail-account" onClick={() => {
              if (guest) return openAuth("in");
              setSide("account");
              requestAnimationFrame(() => document.querySelector(".right")?.scrollIntoView({ behavior: "smooth", block: "start" }));
            }} aria-label={guest ? "Sign in" : "Account"} data-tip={guest ? "Sign in or create an account" : "Account and sign-ins"}>
            {guest ? <LogIn size={18} strokeWidth={1.8} aria-hidden /> : <UserRound size={18} strokeWidth={1.8} aria-hidden />}
            <span className="rail-label">{guest ? "Sign in" : "Account"}</span>
          </button>
        )}
        <button className="rail-btn rail-theme" onClick={() => comfort.setTheme(comfort.theme === "light" ? "dark" : "light")}
          aria-label={comfort.theme === "light" ? "Switch to dark theme" : "Switch to light theme"} data-tip={comfort.theme === "light" ? "Dark theme" : "Light theme"}>
          {comfort.theme === "light" ? <Moon size={18} strokeWidth={1.8} aria-hidden /> : <Sun size={18} strokeWidth={1.8} aria-hidden />}
          <span className="rail-label">Theme</span>
        </button>
      </nav>
      <div className="shell">
      <TopBar comfort={comfort} guest={guest} openAuth={openAuth} status={status} level={level} setLevel={setLevel} onChanged={refresh} notify={setToast} tick={tick}
        openPalette={() => setPalette(true)} openConnections={() => setCenter("connections")} openBroker={() => setSide("broker")} />
      <CommandMenu open={palette} setOpen={setPalette} actions={actions} />
      <Toaster theme={comfort.theme} position="bottom-right" closeButton toastOptions={{ className: "sd-toast" }} />
      {!comfort.focus && screen && screen.results.length > 0 && <TVTape key={themeKey} symbols={screen.results.map((r) => r.symbol)} />}
      {comfort.focus && <FocusStrip step={nextStep} onGo={"go" in nextStep ? nextStep.go : undefined} />}
      {screen && !screen.ruleset.version && !comfort.focus && (
        <div className="banner">
          Rules are an unconfirmed draft. Review the definitions in <b>Strategy</b> and save them as a version before placing paper trades.
        </div>
      )}
      <div className="workspace">
        <aside className="left" ref={leftScroll}>
          {center !== "portfolio" && <AccountCard status={status} />}
          <Watchlist screen={screen} live={live} selected={selected} onChanged={refresh} notify={setToast}
            onSelect={(s) => { setSelected(s); setCenter("evidence"); }} />
        </aside>
        <main className="center">
          {/* Hidden (not unmounted) while day trading, so the daily chart isn't torn down and rebuilt. */}
          <section className="chart-card card" style={center === "day" || center === "crypto" || center === "connections" || center === "portfolio" ? { display: "none" } : undefined}>
            {/* Keep the chart mounted across refreshes and mode switches; show the empty state on top of it. */}
            <div className="chart-bar">
              <div className="seg" role="group" aria-label="Chart source">
                <button className={chartSrc === "rules" ? "on" : ""} onClick={() => { pickChart("rules"); if (!["D", "W", "M"].includes(tf.interval)) pickTf({ interval: "D" }); }}
                  title="Tickmark's chart: completed bars, rule markers and the planned stop">Rules</button>
                <button className={chartSrc === "tv" ? "on" : ""} onClick={() => pickChart("tv")} title="TradingView's chart: live (15-minute delayed) prices and indicators">TradingView</button>
              </div>
              <div className="seg" role="group" aria-label="Candle size">
                {(["5", "15", "60", "D", "W", "M"] as Interval[]).map((i) => (
                  <button key={i} className={tf.interval === i ? "on" : ""} onClick={() => pickTf({ interval: i })}
                    title={["D", "W", "M"].includes(i) ? undefined : "Intraday candles open in the TradingView view"}>
                    {({ "5": "5m", "15": "15m", "60": "1h", D: "1D", W: "1W", M: "1M" } as Record<string, string>)[i]}</button>
                ))}
              </div>
              <button className="btn small ghost tv-open" onClick={() => openMyTradingView(ev?.symbol ?? selected, tf.interval, tvLayout)}
                title="Your own TradingView (sign in there once): saved layouts, indicators and drawings, in a window beside the desk">
                <ExternalLink size={13} aria-hidden /> My TradingView</button>
              {["D", "W", "M"].includes(tf.interval) && (
                <div className="seg" role="group" aria-label="Time range">
                  {(["1M", "3M", "6M", "1Y", "2Y"] as Range[]).map((r) => (
                    <button key={r} className={tf.range === r ? "on" : ""} onClick={() => pickTf({ range: r })}>{r}</button>
                  ))}
                </div>
              )}
            </div>
            <div className="chart-body" style={chartSrc === "tv" ? { display: "none" } : undefined}>
              <PriceChart key={themeKey} symbol={ev?.symbol ?? selected} tick={tick} evaluation={ev} journal={journal} live={live}
                interval={["D", "W", "M"].includes(tf.interval) ? tf.interval : "D"} range={tf.range} />
              {!ev && <div className="empty overlay">{screen?.needs_refresh ? "No live data yet. Click Refresh live data in the top bar." : "Loading…"}</div>}
            </div>
            {chartSrc === "tv" && <div className="chart-body"><TVChart key={themeKey} symbol={ev?.symbol ?? selected} interval={tf.interval} range={tf.range} /></div>}
          </section>
          <div className="center-head">
            {(() => { const Icon = CICON[center]; return <h2><Icon size={16} aria-hidden /> {CENTER[center]}</h2>; })()}
          </div>
          <AnimatePresence mode="wait" initial={false}>
            <motion.section key={center} ref={centerScroll} className="center-body" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
              exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.14, ease: [0.2, 0.8, 0.2, 1] }}>
              {center === "portfolio" && <Portfolio tick={tick} notify={setToast} />}
              {center === "evidence" && ev && <Evidence ev={ev} level={level} ruleset={screen!.ruleset} tick={tick} />}
              {center === "journal" && <Journal entries={journal} level={level} onChanged={refresh} tick={tick} />}
              {center === "forward" && <Forward tick={tick} level={level} live={live} />}
              {center === "documents" && <Documents symbol={selected} />}
              {center === "connections" && <Connections notify={setToast} goTab={(t) => setSide(t as SideTab)}
                openTicket={(sym) => { setSelected(sym); setCenter("evidence"); setSide("order"); }} symbol={ev?.symbol ?? selected} />}
              {center === "day" && <DayTrading key={themeKey} notify={setToast} onChanged={refresh} />}
              {center === "crypto" && <Crypto notify={setToast} prefill={cryptoPrefill} onPrefillUsed={() => setCryptoPrefill(null)} />}
            </motion.section>
          </AnimatePresence>
        </main>
        <aside className="right">
          <nav className="tabs">
            {(Object.keys(SIDE) as SideTab[]).map((t) => {
              const Icon = SICON[t];
              const flag = (t === "broker" && status?.reconciliation && !status.reconciliation.ok) || (t === "risk" && status?.kill_switch?.on);
              const n = t === "orders" ? orders.filter((o) => o.purpose === "entry").length : 0;
              return (
                <button key={t} className={`${side === t ? "on" : ""} ${flag ? "flag" : ""}`} onClick={() => setSide(t)} title={SIDE[t]}
                  aria-current={side === t ? "page" : undefined}>
                  <Icon size={14} aria-hidden /> <span className="tab-label">{SIDE[t]}</span>{n > 0 && <span className="count">{n}</span>}
                  {flag && <span className="flagdot" aria-label="needs attention" />}
                </button>
              );
            })}
          </nav>
          <AnimatePresence mode="wait" initial={false}>
            <motion.div key={side} ref={sideScroll} className="side-body" initial={{ opacity: 0, x: 6 }} animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0 }} transition={{ duration: 0.14, ease: [0.2, 0.8, 0.2, 1] }}>
            {side === "copilot" && (
              <Copilot level={level} notify={setToast} onChanged={refresh} openDay={() => setCenter("day")}
                openCrypto={(form) => { setCryptoPrefill(form); setCenter("crypto"); }}
                openTicket={(sym, overrides) => { setSelected(sym); setCenter("evidence"); setPrefill({ symbol: sym, overrides, n: Date.now() }); setSide("order"); }} />
            )}
            {side === "strategy" && <StrategyPanel notify={setToast} onSaved={() => { refresh(); setToast("Rule set saved. The watchlist was re-screened."); }} />}
            {side === "order" && status && (
              <OrderTicket ev={ev} ruleset={screen?.ruleset ?? null} status={status} prefill={prefill} onPrefillUsed={() => setPrefill(null)}
                onSubmitted={() => { refresh(); }} notify={setToast} goOrders={() => setSide("orders")} />
            )}
            {side === "orders" && <OrdersPanel orders={orders} onChanged={refresh} notify={setToast} />}
            {side === "broker" && <BrokerPanel onChanged={refresh} notify={setToast} />}
            {side === "risk" && <RiskPanel onChanged={refresh} notify={setToast} />}
            {side === "account" && <AccountPanel user={user} notify={setToast} adminUnclaimed={adminUnclaimed} social={social} />}
            </motion.div>
          </AnimatePresence>
        </aside>
      </div>
      </div>
    </div>
    </MotionConfig>
  );
}
