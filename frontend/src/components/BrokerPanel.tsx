import { useEffect, useState } from "react";
import { LinkedAccounts } from "./LinkedAccounts";
import { api, ApiError, Reconciliation } from "../api";
import { money } from "./ui";

interface Conn {
  provider: string; connected: boolean; key_hint: string | null; source: string | null; base_url: string;
  broker_mode: string; data_mode: string; last_sync: { at: string; synced: number; errors: string[] } | null;
  reconciliation: Reconciliation | null; account_error?: string;
  account?: { equity: number; cash: number; buying_power: number; status: string; trading_blocked: boolean;
    account_blocked: boolean; pattern_day_trader: boolean; daytrade_count: number; account_number: string };
}

const BREAK_LABEL: Record<string, string> = {
  untracked_position: "Position not in journal", missing_position: "Journal position missing at broker",
  qty_mismatch: "Quantity mismatch", unprotected_position: "No stop protecting position",
  stale_local_order: "Order missing at broker", unknown_broker_order: "Broker order not from Tickmark",
  no_planned_exit: "No planned exit", orphan_order: "Order on a closed position",
};

function BrokerSwitch({ which, setWhich }: { which: string; setWhich: (w: "alpaca" | "ibkr" | "oauth") => void }) {
  return (
    <div className="seg brokerswitch" role="group" aria-label="Broker">
      <button className={which === "alpaca" ? "on" : ""} onClick={() => setWhich("alpaca")}>Alpaca</button>
      <button className={which === "ibkr" ? "on" : ""} onClick={() => setWhich("ibkr")}>Interactive Brokers</button>
      <button className={which === "oauth" ? "on" : ""} onClick={() => setWhich("oauth")}>Sign-in brokers</button>
    </div>
  );
}

export function ReconcileCard({ rec, onRun, busy }: { rec: Reconciliation | null; onRun: () => void; busy: boolean }) {
  return (
    <div className={`card recon ${rec ? (rec.ok ? "ok" : "bad") : ""}`}>
      <div className="row between">
        <b>Reconciliation</b>
        <button className="btn small" disabled={busy} onClick={onRun}>{busy ? "Checking…" : "Run now"}</button>
      </div>
      {!rec && <p className="small muted">Not run yet. It also runs after every data refresh.</p>}
      {rec && (
        <>
          <p className="small">{rec.ok ? "✓ In sync" : rec.error ? `Couldn't reach the broker: ${rec.error}` : `${rec.breaks.length} break(s)`}
            <span className="muted"> · {rec.mode === "alpaca_paper" ? "broker vs journal" : "simulator self-check"} · {rec.checked} symbol(s) · {new Date(rec.at).toLocaleString()}</span></p>
          {rec.breaks.length > 0 && (
            <ul className="breaks">{rec.breaks.map((b, i) => (
              <li key={i}><b>{b.symbol}</b> {BREAK_LABEL[b.type] ?? b.type}<span className="muted small"> — {b.detail}</span></li>))}</ul>
          )}
        </>
      )}
    </div>
  );
}

interface BrokerInfo { id: string; name: string; segment: string; connect: string; connect_label: string; paper: boolean; status: string; phase: number; assets: string; how: string }

function Catalogue() {
  const [list, setList] = useState<BrokerInfo[]>([]);
  useEffect(() => { api.get<BrokerInfo[]>("/api/brokers").then(setList); }, []);
  return (
    <details className="catalogue">
      <summary>All brokers and exchanges ({list.filter((b) => b.status === "available").length} trading, {list.filter((b) => b.status === "linked").length} read-only, {list.filter((b) => b.status === "planned").length} planned)</summary>
      <ul>
        {list.map((b) => (
          <li key={b.id}>
            <div className="row between"><b>{b.name}</b>
              <span className={`badge ${b.status === "available" ? "ok" : b.status === "linked" ? "info" : ""}`}>{b.status === "available" ? "Trading" : b.status === "linked" ? "Read-only link" : `Planned · phase ${b.phase}`}</span></div>
            <div className="small muted">{b.segment} · {b.connect_label}{b.paper ? " · paper account" : " · no API paper account"} · {b.assets}</div>
            <div className="small">{b.how}</div>
          </li>
        ))}
      </ul>
    </details>
  );
}

interface Gate {
  eligible: boolean; enabled: boolean; environment: string; live_keys: boolean; live_key_hint: string | null;
  confirm_phrase: string; enabled_at: string | null; checks: { name: string; ok: boolean; detail: string }[];
  paper_record?: { name: string; detail: string }[]; broker?: string;
}

function LiveGate({ onChanged, notify }: { onChanged: () => void; notify: (s: string) => void }) {
  const [g, setG] = useState<Gate | null>(null);
  const [key, setKey] = useState("");
  const [secret, setSecret] = useState("");
  const [phrase, setPhrase] = useState("");
  const [broker, setBroker] = useState<"alpaca" | "ibkr">("alpaca");
  const load = () => api.get<Gate>("/api/live-gate").then(setG);
  useEffect(() => { load(); }, []);
  async function act(fn: () => Promise<unknown>, msg: string) {
    try { await fn(); notify(msg); } catch (e) { notify((e as ApiError).message); } finally { load(); onChanged(); }
  }
  if (!g) return null;
  const ready = g.eligible && (broker === "ibkr" || g.live_keys);
  return (
    <details className={`card gate ${g.enabled ? "on" : ""}`} open={g.enabled}>
      <summary><b>{g.enabled ? `REAL MONEY trading is ON (${g.broker === "ibkr" ? "Interactive Brokers" : "Alpaca"})` : "Real-money trading (stocks)"}</b></summary>
      <p className="small muted">Per-account live switch. Real-money orders go to their own journal, each order needs the ticker typed to confirm,
        and the kill switch and risk limits apply. Crypto exchange accounts have their own switch under Linked accounts.</p>
      <ul className="gatechecks">{g.checks.map((c) => <li key={c.name} className={c.ok ? "ok" : "no"}>{c.ok ? "✓" : "✕"} {c.name} <span className="muted">— {c.detail}</span></li>)}</ul>
      {g.paper_record && <p className="small muted">Paper record (for your judgement, not required): {g.paper_record.map((r) => `${r.name} ${r.detail}`).join(" · ")}</p>}
      {g.enabled ? (
        <button className="btn" onClick={() => act(() => api.post("/api/live-gate/disable"), "Back to paper trading.")}>Switch back to paper</button>
      ) : (
        <div className="col">
          <label className="lbl">Account</label>
          <div className="seg">{(["alpaca", "ibkr"] as const).map((b) => <button type="button" key={b} className={broker === b ? "on" : ""} onClick={() => setBroker(b)}>
            {b === "alpaca" ? "Alpaca live" : "IBKR live"}</button>)}</div>
          {broker === "alpaca" && !g.live_keys && (
            <form onSubmit={(e) => { e.preventDefault(); act(() => api.post("/api/live-gate/keys", { key_id: key, secret }), "Live keys tested and saved."); setKey(""); setSecret(""); }}>
              <label className="lbl">Live API key ID</label><input autoComplete="off" value={key} onChange={(e) => setKey(e.target.value)} />
              <label className="lbl">Live secret key</label><input type="password" autoComplete="new-password" value={secret} onChange={(e) => setSecret(e.target.value)} />
              <button className="btn" disabled={!key || !secret}>Test and save live keys</button>
            </form>
          )}
          {broker === "alpaca" && g.live_keys && <p className="small">Live keys {g.live_key_hint}.</p>}
          {broker === "ibkr" && <p className="small muted">Log in to your live IBKR account at the Client Portal Gateway, then pick it in the IBKR panel after enabling.</p>}
          <p className="small">Type to confirm: <code>{g.confirm_phrase}</code></p>
          <input value={phrase} onChange={(e) => setPhrase(e.target.value)} aria-label="Confirmation phrase" />
          <button className="btn danger" style={{ marginTop: 8 }} disabled={!ready || phrase !== g.confirm_phrase}
            onClick={() => act(() => api.post("/api/live-gate/enable", { phrase, broker }), "REAL-MONEY trading enabled.")}>Enable real-money trading</button>
        </div>
      )}
    </details>
  );
}

interface IBKRState {
  settings: { gateway_url: string; account_id: string | null }; configured: boolean; paper_only: boolean; error?: string;
  session?: { authenticated: boolean; connected: boolean; established: boolean; competing: boolean };
  accounts?: { id: string; paper: boolean; desc?: string }[];
  account?: { equity: number; buying_power: number; status: string; account_number: string };
}

function IBKRPanel({ onChanged, notify, mode, dataMode }: { onChanged: () => void; notify: (s: string) => void; mode: string; dataMode: string }) {
  const [s, setS] = useState<IBKRState | null>(null);
  const [url, setUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const load = () => { setBusy(true); return api.get<IBKRState>("/api/broker/ibkr").then((x) => { setS(x); setUrl(x.settings.gateway_url); }).finally(() => setBusy(false)); };
  useEffect(() => { load(); }, []);
  async function pick(account_id: string) {
    try { setS(await api.put<IBKRState>("/api/broker/ibkr", { account_id, gateway_url: url })); notify(`IBKR paper account ${account_id} selected.`); onChanged(); }
    catch (e) { notify((e as ApiError).message); }
  }
  async function use() {
    try { await api.put("/api/broker/mode", { mode: "ibkr" }); notify("Orders now go to Interactive Brokers (paper)."); onChanged(); }
    catch (e) { notify((e as ApiError).message); }
  }
  if (!s) return <p className="muted small">Checking the IBKR gateway…</p>;
  const ok = s.session?.authenticated && s.session?.connected;
  return (
    <div className="ibkr">
      <ol className="small steps">
        <li>Download IBKR's <b>Client Portal Gateway</b>, unzip it and run <code>bin/run.sh root/conf.yaml</code>.</li>
        <li>Open <code>https://localhost:5000</code> and log in with your <b>paper</b> username. IBKR handles the password and two-factor; Tickmark never sees them.</li>
        <li>Click <b>Check connection</b> below and pick your paper account (IDs start with DU).</li>
      </ol>
      <label className="lbl">Gateway URL</label>
      <div className="row"><input className="grow" value={url} onChange={(e) => setUrl(e.target.value)} />
        <button className="btn small" disabled={busy} onClick={load}>{busy ? "Checking…" : "Check connection"}</button></div>
      {s.error && <p className="err small">{s.error}</p>}
      {s.session && (
        <p className={`small ${ok ? "gain" : "warnc"}`}>Gateway {ok ? "logged in" : "reachable but not logged in"}
          {s.session.competing ? " · another session is competing for this login" : ""}</p>
      )}
      {(s.accounts ?? []).length > 0 && (
        <ul className="accts">{s.accounts!.map((a) => (
          <li key={a.id} className="row between"><span><b className="num">{a.id}</b> <span className="muted small">{a.paper ? "paper" : "LIVE (not allowed in this build)"}</span></span>
            {s.settings.account_id === a.id ? <span className="badge ok">selected</span>
              : <button className="btn small" disabled={!a.paper} onClick={() => pick(a.id)}>Use</button>}</li>))}</ul>
      )}
      {s.account && (
        <dl className="figs"><dt>Equity</dt><dd className="num">{money(s.account.equity)}</dd><dt>Buying power</dt><dd className="num">{money(s.account.buying_power)}</dd></dl>
      )}
      {s.configured && mode !== "ibkr" && (
        <button className="btn small primary" disabled={dataMode !== "live"} onClick={use}
          title={dataMode !== "live" ? "Switch to Live data first" : ""}>Send orders to Interactive Brokers</button>
      )}
      <p className="small muted">Planned exits go out as market orders at 3:40 PM ET (market-on-close isn't confirmed for IBKR's Web API). Built against IBKR's documented Web API and a test gateway; not yet run against a real IBKR session.</p>
    </div>
  );
}

interface OAuthState { id: string; name: string; configured: boolean; connected: boolean; note: string; redirect_uri: string; client_id_env: string; relogin_in_days?: number | null }

function OAuthList({ notify }: { notify: (s: string) => void }) {
  const [list, setList] = useState<OAuthState[]>([]);
  const load = () => api.get<OAuthState[]>("/api/oauth").then(setList);
  useEffect(() => { load(); const t = setInterval(load, 5000); return () => clearInterval(t); }, []);
  async function connect(p: OAuthState) {
    try { const r = await api.get<{ authorize_url: string }>(`/api/oauth/${p.id}/start`); window.open(r.authorize_url, "_blank", "noopener"); }
    catch (e) { notify((e as ApiError).message); }
  }
  async function disconnect(p: OAuthState) {
    await fetch(`/api/oauth/${p.id}`, { method: "DELETE" }); load();
  }
  return (
    <div className="oauthlist">
      <p className="small muted">Sign-in brokers: click Connect, log in on the broker's own site, and Tickmark keeps the session renewed. Order routing for these arrives with their adapters later in Phase 2.</p>
      <ul>{list.map((p) => (
        <li key={p.id}>
          <div className="row between"><b>{p.name}</b>
            {p.connected
              ? <span className="row"><span className="badge ok">connected{p.relogin_in_days != null ? ` · re-login in ${p.relogin_in_days}d` : ""}</span>
                  <button className="btn small ghost" onClick={() => disconnect(p)}>Disconnect</button></span>
              : <button className="btn small" disabled={!p.configured} onClick={() => connect(p)} title={p.configured ? "" : `Set ${p.client_id_env} on the server`}>Connect with {p.name}</button>}
          </div>
          {!p.configured && <div className="small muted">Needs app registration: {p.note} Then set <code>{p.client_id_env}</code> on the server.</div>}
        </li>))}</ul>
    </div>
  );
}

export function BrokerPanel({ onChanged, notify }: { onChanged: () => void; notify: (s: string) => void }) {
  const [c, setC] = useState<Conn | null>(null);
  const [key, setKey] = useState("");
  const [secret, setSecret] = useState("");
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => api.get<Conn>("/api/broker/connection").then(setC);
  useEffect(() => { load(); }, []);

  async function run(label: string, fn: () => Promise<void>) {
    setBusy(label);
    try { await fn(); } catch (e) { notify((e as ApiError).message); } finally { setBusy(null); load(); onChanged(); }
  }
  const connect = () => run("connect", async () => {
    await api.post("/api/broker/connect", { key_id: key, secret });
    setKey(""); setSecret("");
    notify("Connected to Alpaca Paper. Keys saved in your system keychain.");
  });
  const disconnect = () => {
    if (!confirm("Disconnect Alpaca? The keys are removed from the keychain and the broker switches back to the local simulator. Orders already at Alpaca stay there.")) return;
    run("disconnect", async () => { await fetch("/api/broker/connect", { method: "DELETE" }); notify("Alpaca disconnected."); });
  };
  const useAlpaca = () => run("mode", async () => { await api.put("/api/broker/mode", { mode: "alpaca_paper" }); notify("Orders now go to Alpaca Paper."); });
  const sync = () => run("sync", async () => { const r = await api.post<any>("/api/broker/sync"); notify(`Synced ${r.synced} order(s).${r.errors.length ? " " + r.errors.join("; ") : ""}`); });
  const exits = () => run("exits", async () => { const r = await api.post<any>("/api/broker/exits"); notify(r.events.length ? r.events.join(" · ") : "No planned exits due."); });
  const reconcile = () => run("recon", async () => { await api.post("/api/reconcile"); });

  const [which, setWhich] = useState<"alpaca" | "ibkr" | "oauth">("alpaca");
  if (!c) return <p className="muted pad">Loading…</p>;
  const a = c.account;
  if (which !== "alpaca") {
    return (
      <div className="brokerp">
        <BrokerSwitch which={which} setWhich={setWhich} />
        {which === "ibkr" ? <IBKRPanel onChanged={() => { load(); onChanged(); }} notify={notify} mode={c.broker_mode} dataMode={c.data_mode} /> : <OAuthList notify={notify} />}
        <ReconcileCard rec={c.reconciliation} onRun={reconcile} busy={busy === "recon"} />
        <LinkedAccounts notify={notify} />
        <LinkedAccounts notify={notify} />
      <Catalogue />
      </div>
    );
  }
  return (
    <div className="brokerp">
      <BrokerSwitch which={which} setWhich={setWhich} />
      <h3>{c.provider}</h3>
      {!c.connected ? (
        <form className="connect" onSubmit={(e) => { e.preventDefault(); connect(); }}>
          <p className="small muted">Paste the API keys from your Alpaca <b>paper</b> dashboard. They're tested, then stored in this computer's keychain. The browser never sees them again. Live-trading keys are refused.</p>
          <label className="lbl" htmlFor="kid">API key ID</label>
          <input id="kid" autoComplete="off" spellCheck={false} value={key} onChange={(e) => setKey(e.target.value)} placeholder="PK…" />
          <label className="lbl" htmlFor="sec">Secret key</label>
          <input id="sec" type="password" autoComplete="new-password" value={secret} onChange={(e) => setSecret(e.target.value)} />
          <button className="btn primary" disabled={!key || !secret || !!busy}>{busy === "connect" ? "Testing connection…" : "Test and save"}</button>
        </form>
      ) : (
        <>
          <div className="card acctcard">
            <div className="row between"><b>Connected</b><span className="muted small">key {c.key_hint} · {c.source}</span></div>
            {a ? (
              <dl className="figs">
                <dt>Account</dt><dd className="num">{a.account_number} · {a.status}{a.trading_blocked || a.account_blocked ? " · BLOCKED" : ""}</dd>
                <dt>Equity</dt><dd className="num">{money(a.equity)}</dd>
                <dt>Buying power</dt><dd className="num">{money(a.buying_power)}</dd>
                <dt>Day trades (5 sessions)</dt><dd className="num">{a.daytrade_count}{a.pattern_day_trader ? " · flagged PDT" : ""}</dd>
              </dl>
            ) : <p className="err small">{c.account_error}</p>}
            <div className="row">
              {c.broker_mode !== "alpaca_paper" && (
                <button className="btn small primary" disabled={c.data_mode !== "live" || !!busy} onClick={useAlpaca}
                  title={c.data_mode !== "live" ? "Switch to Live data first: broker orders must be sized from real prices" : ""}>Send orders to Alpaca</button>
              )}
              <button className="btn small" disabled={!!busy} onClick={sync}>Sync orders</button>
              <button className="btn small" disabled={!!busy || c.broker_mode !== "alpaca_paper"} onClick={exits}>Send due exits</button>
              <button className="btn small ghost" disabled={!!busy} onClick={disconnect}>Disconnect</button>
            </div>
            {c.data_mode !== "live" && <p className="small warnc">Orders can only go to Alpaca in Live data mode.</p>}
            {c.last_sync && <p className="small muted">Last sync {new Date(c.last_sync.at).toLocaleString()} · {c.last_sync.synced} order(s){c.last_sync.errors.length ? ` · ${c.last_sync.errors.length} error(s)` : ""}</p>}
          </div>
          <p className="small muted">Planned exits are sent automatically at 3:40 PM ET as market-on-close orders (the stop is canceled first so the shares are free). If that's missed, a market exit goes out at the next run. Order status syncs on every refresh.</p>
        </>
      )}
      <ReconcileCard rec={c.reconciliation} onRun={reconcile} busy={busy === "recon"} />
      <LiveGate onChanged={() => { load(); onChanged(); }} notify={notify} />
      <LinkedAccounts notify={notify} />
      <Catalogue />
    </div>
  );
}
