import { useCallback, useEffect, useState } from "react";
import { api, Evaluation, Order, Ruleset, Status_ } from "./api";
import { TopBar } from "./components/TopBar";
import { Watchlist } from "./components/Watchlist";
import { PriceChart } from "./components/PriceChart";
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
import { BarChart3, BookOpen, Bot, Briefcase, CandlestickChart, ClipboardList, FileText, Gauge, Bitcoin, ListChecks, Plug, ShieldCheck, Target, Timer, UserRound } from "lucide-react";

export type Level = "beginner" | "analyst";
type CenterTab = "evidence" | "journal" | "forward" | "documents" | "day" | "crypto" | "connections";
type SideTab = "copilot" | "strategy" | "order" | "orders" | "broker" | "risk" | "account";

export interface Screen {
  as_of: string | null; ruleset: Ruleset; results: Evaluation[]; errors: Record<string, string>; data_mode: string; needs_refresh: boolean;
  quality?: Record<string, { ok: boolean; issues: { kind: string; date: string | null; detail: string }[] }>; data_provider?: string | null;
}

export default function App() {
  const [auth, setAuth] = useState<{ portal: boolean; user: PortalUser | null; first_user?: boolean; open_signup?: boolean } | null>(null);
  const load = () => api.get<any>("/api/auth/status").then(setAuth).catch(() => setAuth({ portal: false, user: null }));
  useEffect(() => {
    load();
    const out = () => setAuth((a) => (a ? { ...a, user: null } : a));
    window.addEventListener("swingdesk:signin", out);
    return () => window.removeEventListener("swingdesk:signin", out);
  }, []);
  if (!auth) return <div className="pad muted">Loading…</div>;
  if (auth.portal && !auth.user) return <Login firstUser={!!auth.first_user} openSignup={!!auth.open_signup} onIn={(u) => setAuth({ ...auth, user: u, first_user: false })} />;
  return <Desk user={auth.portal ? auth.user : null} key={auth.user?.id ?? "local"} />;
}

function Desk({ user }: { user: PortalUser | null }) {
  const [status, setStatus] = useState<Status_ | null>(null);
  const [screen, setScreen] = useState<Screen | null>(null);
  const [orders, setOrders] = useState<Order[]>([]);
  const [journal, setJournal] = useState<any[]>([]);
  const [selected, setSelected] = useState("NVDA");
  const [level, setLevel] = useState<Level>("beginner");
  const [center, setCenter] = useState<CenterTab>("evidence");
  const [side, setSide] = useState<SideTab>("strategy");
  const [tick, setTick] = useState(0);
  // Toasts through sonner: same notify(string) API the panels already use.
  const setToast = useCallback((m: string | null) => {
    if (!m) return;
    const bad = /couldn't|can't|failed|refused|blocked|error|not enabled|invalid|rejected|denied|expired/i.test(m);
    (bad ? sonner.error : sonner)(m, { duration: bad ? 9000 : 5000 });
  }, []);
  const [palette, setPalette] = useState(false);
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


  // Keep the selection valid when the live watchlist changes.
  useEffect(() => {
    if (screen?.results.length && !screen.results.some((r) => r.symbol === selected)) setSelected(screen.results[0].symbol);
  }, [screen, selected]);

  const ev = screen?.results.find((r) => r.symbol === selected) ?? null;
  const live = status?.data_mode === "live";
  const CENTER: Record<CenterTab, string> = { evidence: "Rule evidence", journal: `Journal (${journal.length})`, forward: live ? "Live forward test" : "Forward test",
    documents: "Documents", day: "Day trading", crypto: "Crypto", connections: "Connections" };
  const CICON: Record<CenterTab, typeof Gauge> = { evidence: ListChecks, journal: BookOpen, forward: Timer, documents: FileText, day: CandlestickChart,
    crypto: Bitcoin, connections: Plug };
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

  return (
    <div className="app">
      <TopBar status={status} level={level} setLevel={setLevel} onChanged={refresh} notify={setToast} tick={tick}
        openPalette={() => setPalette(true)} openConnections={() => setCenter("connections")} openBroker={() => setSide("broker")} />
      <CommandMenu open={palette} setOpen={setPalette} actions={actions} />
      <Toaster theme="dark" position="bottom-right" closeButton toastOptions={{ className: "sd-toast" }} />
      {screen && !screen.ruleset.version && (
        <div className="banner">
          Rules are an unconfirmed draft. Review the definitions in <b>Strategy</b> and save them as a version before placing paper trades.
        </div>
      )}
      <div className="workspace">
        <aside className="left">
          <Watchlist screen={screen} live={live} selected={selected} onChanged={refresh} notify={setToast}
            onSelect={(s) => { setSelected(s); setCenter("evidence"); }} />
        </aside>
        <main className="center">
          {/* Hidden (not unmounted) while day trading, so the daily chart isn't torn down and rebuilt. */}
          <section className="chart-card card" style={center === "day" || center === "crypto" || center === "connections" ? { display: "none" } : undefined}>
            {/* Keep the chart mounted across refreshes and mode switches; show the empty state on top of it. */}
            <PriceChart symbol={ev?.symbol ?? selected} tick={tick} evaluation={ev} journal={journal} live={live} />
            {!ev && <div className="empty overlay">{screen?.needs_refresh ? "No live data yet. Click Refresh live data in the top bar." : "Loading…"}</div>}
          </section>
          <nav className="tabs">
            {(Object.keys(CENTER) as CenterTab[]).map((t) => {
              const Icon = CICON[t];
              return (
                <button key={t} className={center === t ? "on" : ""} onClick={() => setCenter(t)} aria-current={center === t ? "page" : undefined}>
                  <Icon size={14} aria-hidden /> {CENTER[t]}
                </button>
              );
            })}
          </nav>
          <section className="center-body">
            {center === "evidence" && ev && <Evidence ev={ev} level={level} ruleset={screen!.ruleset} tick={tick} />}
            {center === "journal" && <Journal entries={journal} level={level} onChanged={refresh} tick={tick} />}
            {center === "forward" && <Forward tick={tick} level={level} live={live} />}
            {center === "documents" && <Documents symbol={selected} />}
            {center === "connections" && <Connections notify={setToast} goTab={(t) => setSide(t as SideTab)} />}
            {center === "day" && <DayTrading notify={setToast} onChanged={refresh} />}
            {center === "crypto" && <Crypto notify={setToast} prefill={cryptoPrefill} onPrefillUsed={() => setCryptoPrefill(null)} />}
          </section>
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
          <div className="side-body">
            {side === "copilot" && (
              <Copilot level={level} notify={setToast} onChanged={refresh} openDay={() => setCenter("day")}
                openCrypto={(form) => { setCryptoPrefill(form); setCenter("crypto"); }}
                openTicket={(sym, overrides) => { setSelected(sym); setCenter("evidence"); setPrefill({ symbol: sym, overrides, n: Date.now() }); setSide("order"); }} />
            )}
            {side === "strategy" && <StrategyPanel onSaved={() => { refresh(); setToast("Rule set saved. The watchlist was re-screened."); }} />}
            {side === "order" && status && (
              <OrderTicket ev={ev} ruleset={screen?.ruleset ?? null} status={status} prefill={prefill} onPrefillUsed={() => setPrefill(null)}
                onSubmitted={() => { refresh(); }} notify={setToast} goOrders={() => setSide("orders")} />
            )}
            {side === "orders" && <OrdersPanel orders={orders} onChanged={refresh} notify={setToast} />}
            {side === "broker" && <BrokerPanel onChanged={refresh} notify={setToast} />}
            {side === "risk" && <RiskPanel onChanged={refresh} notify={setToast} />}
            {side === "account" && <AccountPanel user={user} notify={setToast} />}
          </div>
        </aside>
      </div>
    </div>
  );
}
