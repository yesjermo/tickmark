import { useState } from "react";
import { CircleDot, Command as CmdIcon, FlaskConical, FastForward, Octagon, Plug, RefreshCw, RotateCcw, ShieldAlert, TestTube2 } from "lucide-react";
import { api, ApiError, Status_ } from "../api";
import type { Level } from "../App";
import { money } from "./ui";
import { AlertsBell } from "./AlertsDrawer";

function ago(iso: string) {
  const m = Math.round((Date.now() - new Date(iso).getTime()) / 60000);
  return m < 1 ? "just now" : m < 60 ? `${m} min ago` : m < 1440 ? `${Math.round(m / 60)} h ago` : `${Math.round(m / 1440)} d ago`;
}

export type Workspace = "backtest" | "test" | "trading";
export const workspaceOf = (s: Status_ | null): Workspace | null => (!s ? null : s.live_trading ? "trading" : s.data_mode === "live" ? "test" : "backtest");

const WS: { id: Workspace; label: string; hint: string; Icon: typeof FlaskConical }[] = [
  { id: "backtest", label: "Backtest", hint: "Simulated sessions and historical checks. Nothing touches a real market.", Icon: FlaskConical },
  { id: "test", label: "Live test", hint: "Real market data, paper orders: the forward test of your rules.", Icon: TestTube2 },
  { id: "trading", label: "Live trading", hint: "Real money, on accounts you switched on in the Broker tab.", Icon: CircleDot },
];

export function TopBar({ status, level, setLevel, onChanged, notify, tick, openPalette, openConnections, openBroker }: {
  status: Status_ | null; level: Level; setLevel: (l: Level) => void; onChanged: () => void; notify: (s: string) => void; tick: number;
  openPalette: () => void; openConnections: () => void; openBroker: () => void;
}) {
  const [busy, setBusy] = useState<string | null>(null);
  const sim = status?.mode === "simulator";
  const live = status?.data_mode === "live";
  const ws = workspaceOf(status);

  async function run(label: string, fn: () => Promise<void>) {
    setBusy(label);
    try { await fn(); } catch (e) { notify((e as ApiError).message); } finally { setBusy(null); onChanged(); }
  }
  const advance = () => run("advance", async () => {
    const r = await api.post<{ as_of: string; events: string[] }>("/api/sim/advance");
    notify(`Simulated ${r.as_of}: ${r.events.join(" · ")}`);
  });
  const refresh = () => run("refresh", async () => {
    const r = await api.post<any>("/api/live/refresh");
    const bad = Object.entries(r.report).filter(([, v]: any) => !v.ok).map(([k, v]: any) => `${k}: ${v.error}`);
    notify(`Live data through ${r.as_of} close.` + (r.events.length ? ` ${r.events.join(" · ")}` : " No new paper fills.")
      + (bad.length ? ` Fetch problems: ${bad.join("; ")}` : ""));
  });
  const reset = () => {
    const msg = live
      ? "Reset the live-test paper account? This clears paper orders, the journal and forward-test signals. Cached market data and rule versions are kept."
      : "Reset the backtest? This clears rules, orders and the journal and rewinds the simulated clock to 2026-11-20.";
    if (!confirm(msg)) return;
    run("reset", async () => { await api.post("/api/sim/reset"); notify(live ? "Live-test paper account reset." : "Backtest reset to 2026-11-20 close."); });
  };
  const switchTo = (to: Workspace) => run("ws", async () => {
    if (to === ws || !ws) return;
    if (to === "trading") {
      openBroker();
      notify("Live trading needs a real-money switch: turn one on in Broker → Real-money trading (stocks) or Linked accounts (crypto).");
      return;
    }
    if (ws === "trading") await api.post("/api/live-gate/disable");
    if (to === "backtest") await api.put("/api/data/mode", { mode: "demo" });
    if (to === "test" && !live) await api.put("/api/data/mode", { mode: "live" });
    notify(to === "backtest" ? "Backtest: simulated sessions you step through by hand. Your live-test record is kept separately."
      : "Live test: real completed bars, paper orders. Your backtest is kept separately.");
  });
  async function setAuto(enabled: boolean) {
    try { await api.put("/api/scheduler", { enabled }); notify(enabled ? "Auto-refresh on: data updates at 16:20 ET each trading day while the server runs." : "Auto-refresh off."); onChanged(); }
    catch (e) { notify((e as ApiError).message); }
  }
  const setProvider = (provider: string) => run("data", async () => {
    const r = await api.put<any>("/api/data/provider", { provider });
    notify(r.refresh?.provider_note ?? `Market data: ${provider === "alpaca" ? "Alpaca SIP consolidated tape (licensed)" : "Yahoo Finance"}. Data through ${r.refresh?.as_of ?? "?"} close.`);
  });
  async function setMode(mode: string) {
    try { await api.put("/api/broker/mode", { mode }); onChanged(); } catch (e) { notify((e as ApiError).message); }
  }

  const lf = status?.live_fetch;
  const sch = status?.scheduler;
  const nextRun = sch?.next_run ? new Date(sch.next_run).toLocaleString("en-US", { weekday: "short", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }) + " ET" : null;
  const failed = lf ? Object.entries(lf.report).filter(([, v]) => !v.ok) : [];
  const mac = typeof navigator !== "undefined" && /Mac/.test(navigator.platform);

  return (
    <header className={`topbar ws-${ws ?? "loading"}`}>
      <div className="tb-row">
        <div className="brand"><span className="logo" aria-hidden>◢</span> SwingDesk</div>

        <nav className="workspaces" aria-label="Workspace">
          {WS.map(({ id, label, hint, Icon }) => (
            <button key={id} className={`ws ${ws === id ? "on" : ""} ws-btn-${id}`} title={hint} aria-pressed={ws === id}
              disabled={busy === "ws"} onClick={() => switchTo(id)}>
              <Icon size={15} strokeWidth={2} aria-hidden /> {label}
            </button>
          ))}
        </nav>

        <div className="tb-spacer" />
        {status?.kill_switch?.on && <div className="kspill" title={status.kill_switch.reason ?? ""}><Octagon size={14} aria-hidden /> Kill switch on</div>}
        <div className="acct" title={ws === "trading" ? "Equity of the live account" : "Paper account equity"}>
          <span className="muted">{ws === "trading" ? "Live equity" : ws === "test" ? "Paper equity" : "Backtest equity"}</span>
          <b className="num">{status ? money(status.account.equity) : "—"}</b>
        </div>
        <button className="iconbtn" onClick={openConnections} title="Connections: brokers, exchanges and AI"><Plug size={16} aria-hidden /><span className="sr">Connections</span></button>
        <AlertsBell tick={tick} notify={notify} />
        <div className="seg" role="group" aria-label="Explanation level">
          {(["beginner", "analyst"] as Level[]).map((l) => (
            <button key={l} className={level === l ? "on" : ""} onClick={() => setLevel(l)}>{l === "beginner" ? "Beginner" : "Analyst"}</button>
          ))}
        </div>
        <button className="cmdk-trigger" onClick={openPalette} title="Command palette"><CmdIcon size={13} aria-hidden /> {mac ? "⌘K" : "Ctrl K"}</button>
      </div>

      <div className="tb-row tb-context">
        {ws === "trading" && <span className="realpill"><ShieldAlert size={14} aria-hidden /> Real money</span>}
        <label className="ctx" title={status?.data_label}>
          <span className="ctx-k">Data</span>
          {live ? (
            <select value={status?.data_provider ?? "yahoo"} onChange={(e) => setProvider(e.target.value)} aria-label="Market data provider" disabled={!!busy}>
              <option value="yahoo">Yahoo Finance (free)</option>
              <option value="alpaca" disabled={!status?.alpaca_configured}>Alpaca SIP (licensed){status?.alpaca_configured ? "" : " (connect Alpaca)"}</option>
            </select>
          ) : <span className="ctx-v">Simulated sessions</span>}
        </label>
        <label className="ctx">
          <span className="ctx-k">Orders</span>
          <select value={status?.mode ?? "simulator"} onChange={(e) => setMode(e.target.value)} aria-label="Order routing">
            <option value="simulator">Local simulator</option>
            <option value="alpaca_paper" disabled={!status?.alpaca_configured}>Alpaca{ws === "trading" ? " (live)" : " paper"}{status?.alpaca_configured ? "" : " (connect first)"}</option>
            <option value="ibkr" disabled={!status?.ibkr_configured}>Interactive Brokers{status?.ibkr_configured ? "" : " (connect first)"}</option>
          </select>
        </label>
        <span className="ctx"><span className="ctx-k">Through</span><b className="num">{status?.as_of ?? "—"}</b><span className="muted">close</span></span>
        {live ? (
          <>
            <button className="btn small" disabled={!!busy} onClick={refresh}><RefreshCw size={13} aria-hidden className={busy === "refresh" ? "spin" : ""} /> {busy === "refresh" ? "Fetching…" : "Refresh"}</button>
            {lf && <span className={`muted small ${failed.length ? "warnc" : ""}`} title={failed.map(([k, v]) => `${k}: ${v.error}`).join("\n")}>
              fetched {ago(lf.fetched_at)}{failed.length ? ` · ${failed.length} failed` : ""}</span>}
            <label className="auto" title={sch?.thread_alive ? "Refreshes after each session closes while the server runs; catches up after sleep." : "Scheduler not running in this server process"}>
              <input type="checkbox" checked={!!sch?.enabled} onChange={(e) => setAuto(e.target.checked)} />
              Auto-refresh {sch?.enabled && nextRun ? <span className="muted">· next {nextRun}</span> : null}
              {sch?.running && <span className="warnc"> · refreshing…</span>}
            </label>
          </>
        ) : (
          sim && <button className="btn small" disabled={!!busy} onClick={advance}><FastForward size={13} aria-hidden /> Next session</button>
        )}
        {status?.stream?.target && (
          <span className={`streampill ${status.stream.connected ? "on" : "off"}`} title={status.stream.last_error ?? "Order updates from the broker"}>
            <span className="dot" /> {status.stream.connected ? (status.stream.transport === "websocket" ? "Order updates live" : "Order updates · polling") : "Reconnecting…"}
          </span>
        )}
        <div className="tb-spacer" />
        {ws !== "trading" && <button className="btn small ghost" disabled={!!busy} onClick={reset}><RotateCcw size={13} aria-hidden /> {live ? "Reset paper account" : "Reset backtest"}</button>}
      </div>
    </header>
  );
}
