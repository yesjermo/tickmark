import { useEffect, useState } from "react";
import { api, ApiError } from "../api";


interface Acct { id: string; label: string; provider_name: string; sandbox: boolean; live: boolean; can_trade: boolean; why: string; environment: string }
interface Check { id: string; name: string; result: "pass" | "block"; observed: string; threshold: string }
interface Preview { id: string; account_label: string; environment: string; symbol: string; base: string; quote: string; side: string;
  order_type: string; amount: number; price: number; est_cost: number; stop_price: number | null; est_loss_if_stopped: number | null;
  bid: number | null; ask: number | null; checks: Check[]; blocked: boolean; stop_note: string; fill_disclaimer: string; expires_at: string;
  live: boolean; confirm_text: string | null }
interface COrder { id: string; symbol: string; side: string; order_type: string; purpose: string; amount: number; status: string; filled: number;
  average: number | null; stop_price: number | null; environment: string; reason: string | null; created_at: string; children: COrder[] }
export interface CryptoForm { account_id: string; symbol: string; side: string; notional: number; stop_price: number | null }

const q = (n: number | null | undefined) => (n == null ? "—" : n.toLocaleString(undefined, { maximumFractionDigits: n < 1 ? 8 : 4 }));

export function Crypto({ notify, prefill, onPrefillUsed }: { notify: (s: string) => void; prefill?: CryptoForm | null; onPrefillUsed?: () => void }) {
  const [accts, setAccts] = useState<Acct[]>([]);
  const [orders, setOrders] = useState<COrder[]>([]);
  const [f, setF] = useState({ account_id: "", symbol: "BTC/USDT", side: "buy", notional: "100", order_type: "market", limit_price: "", stop_price: "" });
  const [pv, setPv] = useState<Preview | null>(null);
  const [thesis, setThesis] = useState("");
  const [liveConfirm, setLiveConfirm] = useState("");
  const [busy, setBusy] = useState(false);

  const loadOrders = () => api.get<COrder[]>("/api/crypto/orders").then(setOrders).catch(() => {});
  useEffect(() => {
    api.get<Acct[]>("/api/crypto/accounts").then((a) => {
      setAccts(a);
      const first = a.find((x) => x.can_trade);
      if (first) setF((v) => ({ ...v, account_id: v.account_id || first.id }));
    });
    loadOrders();
    const t = setInterval(loadOrders, 15000);
    return () => clearInterval(t);
  }, []);
  useEffect(() => {
    if (!prefill) return;
    setF((v) => ({ ...v, account_id: prefill.account_id, symbol: prefill.symbol, side: prefill.side, notional: String(prefill.notional),
      stop_price: prefill.stop_price != null ? String(prefill.stop_price) : "", order_type: "market" }));
    setPv(null); onPrefillUsed?.();
    notify("Copilot proposal loaded. Preview it, then confirm.");
  }, [prefill]);

  async function preview() {
    setBusy(true); setPv(null);
    try {
      setPv(await api.post<Preview>("/api/crypto/preview", {
        account_id: f.account_id, symbol: f.symbol, side: f.side, notional: Number(f.notional) || null, order_type: f.order_type,
        limit_price: f.order_type === "limit" ? Number(f.limit_price) || null : null, stop_price: f.stop_price ? Number(f.stop_price) : null }));
    } catch (e) { notify((e as ApiError).message); } finally { setBusy(false); }
  }
  async function submit() {
    if (!pv || busy) return;
    setBusy(true);
    try {
      await api.post("/api/crypto/orders", { preview_id: pv.id, confirm: true, thesis, live_confirm: liveConfirm });
      setLiveConfirm("");
      notify(`Sent to ${pv.account_label} (${pv.environment}).`); setPv(null); setThesis(""); loadOrders();
    } catch (e) { notify((e as ApiError).message); } finally { setBusy(false); }
  }
  async function act(path: string, msg: string) {
    try { await api.post(path); notify(msg); } catch (e) { notify((e as ApiError).message); } finally { loadOrders(); }
  }

  const tradable = accts.filter((a) => a.can_trade);
  return (
    <div className="crypto">
      {accts.find((a) => a.id === f.account_id)?.live
        ? <div className="mode-strip real">REAL MONEY · {accts.find((a) => a.id === f.account_id)?.label.toUpperCase()}</div>
        : <div className="mode-strip simulator">CRYPTO · TESTNET / DEMO</div>}
      {tradable.length === 0 ? (
        <div className="card pad small">
          <p><b>No exchange account can trade yet.</b> Link one in the Broker tab (Linked accounts): a testnet / demo account trades
            right away; a real account (OKX, Coinbase, Kraken, Robinhood Crypto) trades after you turn on its real-money switch there.</p>
          {accts.length > 0 && <p className="muted">Linked but read-only: {accts.map((a) => `${a.label} (${a.why})`).join("; ")}</p>}
        </div>
      ) : (
        <div className="cgrid">
          <form className="card pad col" onSubmit={(e) => { e.preventDefault(); preview(); }}>
            <label>Account <select value={f.account_id} onChange={(e) => setF({ ...f, account_id: e.target.value })}>
              {tradable.map((a) => <option key={a.id} value={a.id}>{a.label} · {a.provider_name} · {a.live ? "REAL MONEY" : "testnet"}</option>)}</select></label>
            <label>Market <input value={f.symbol} onChange={(e) => setF({ ...f, symbol: e.target.value.toUpperCase() })} placeholder="BTC/USDT" /></label>
            <div className="seg">{["buy", "sell"].map((s) => <button type="button" key={s} className={f.side === s ? "on" : ""} onClick={() => setF({ ...f, side: s })}>{s}</button>)}</div>
            <label>Size (quote currency) <input inputMode="decimal" value={f.notional} onChange={(e) => setF({ ...f, notional: e.target.value })} /></label>
            <label>Type <select value={f.order_type} onChange={(e) => setF({ ...f, order_type: e.target.value })}>
              <option value="market">Market</option><option value="limit">Limit</option></select></label>
            {f.order_type === "limit" && <label>Limit price <input inputMode="decimal" value={f.limit_price} onChange={(e) => setF({ ...f, limit_price: e.target.value })} /></label>}
            {f.side === "buy" && <label>Protective stop (optional) <input inputMode="decimal" value={f.stop_price} placeholder="placed at the exchange after the fill"
              onChange={(e) => setF({ ...f, stop_price: e.target.value })} /></label>}
            <button className="btn primary" disabled={busy || !f.account_id}>{busy && !pv ? "Pricing…" : "Preview order"}</button>
          </form>
          {pv && (
            <div className="card pad dayticket">
              <div className={`mode-strip ${pv.live ? "real" : "simulator"}`}>{pv.environment.toUpperCase()} · {pv.account_label.toUpperCase()}</div>
              <dl className="figs">
                <dt>{pv.side === "buy" ? "Buy" : "Sell"}</dt><dd className="num">{q(pv.amount)} {pv.base} {pv.order_type === "market" ? "at market" : `limit ${pv.price}`}</dd>
                <dt>Price</dt><dd className="num">~{q(pv.price)} {pv.quote} <small>bid {q(pv.bid)} / ask {q(pv.ask)}</small></dd>
                <dt>Est. value</dt><dd className="num">{q(pv.est_cost)} {pv.quote}</dd>
                {pv.stop_price != null && <><dt>Stop</dt><dd className="num">{q(pv.stop_price)} <small>loss if stopped ~{q(pv.est_loss_if_stopped)} {pv.quote}</small></dd></>}
              </dl>
              <ul className="risklist-mini">{pv.checks.map((c) => <li key={c.id} className={`rk-${c.result}`}>{c.result === "pass" ? "✓" : "✕"} {c.name}
                <span className="muted small"> {c.observed} ({c.threshold})</span></li>)}</ul>
              <p className="small muted">{pv.stop_note}</p>
              <p className="disclaimer">{pv.fill_disclaimer} Preview expires in 2 minutes.</p>
              <textarea rows={2} placeholder="Thesis (optional, saved with the order)" value={thesis} onChange={(e) => setThesis(e.target.value)} />
              {pv.live && <label className="small">Real money: type <b>{pv.confirm_text}</b> to confirm
                <input value={liveConfirm} onChange={(e) => setLiveConfirm(e.target.value)} aria-label="Type the asset to confirm" /></label>}
              <div className="row">
                <button className={`btn ${pv.live ? "danger" : "primary"}`} disabled={pv.blocked || busy || (pv.live && liveConfirm.trim().toUpperCase() !== (pv.confirm_text ?? "").toUpperCase())}
                  onClick={submit}>{pv.blocked ? "Blocked by checks" : pv.live ? `Send REAL-MONEY order to ${pv.account_label}` : `Send to ${pv.account_label}`}</button>
                <button className="btn ghost" onClick={() => setPv(null)}>Cancel</button>
              </div>
            </div>
          )}
        </div>
      )}
      <h4>Crypto orders</h4>
      {orders.length === 0 ? <p className="small muted">None yet.</p> : (
        <table className="mini"><thead><tr><th>Market</th><th>Side</th><th>Amount</th><th>Status</th><th>Avg</th><th>Stop</th><th></th></tr></thead>
          <tbody>{orders.map((o) => {
            const stop = o.children.find((c) => c.purpose === "stop");
            const exit = o.children.find((c) => c.purpose === "exit");
            return (
              <tr key={o.id} title={o.reason ?? ""}><td>{o.symbol}</td><td>{o.side}</td><td className="num">{q(o.filled || o.amount)}</td>
                <td>{exit ? `closed (${exit.status})` : o.status}</td><td className="num">{q(o.average)}</td>
                <td className="small">{stop ? `${q(stop.stop_price)} · ${stop.status}` : o.stop_price ? `${q(o.stop_price)} · after fill` : "—"}</td>
                <td>{["pending", "open", "partially_filled"].includes(o.status) && <button className="btn small ghost" onClick={() => act(`/api/crypto/orders/${o.id}/cancel`, "Cancel sent.")}>Cancel</button>}
                  {o.side === "buy" && o.filled > 0 && !exit && stop?.status !== "filled" &&
                    <button className="btn small ghost" onClick={() => window.confirm(`Sell ${q(o.filled)} ${o.symbol.split("/")[0]} at market?`) && act(`/api/crypto/orders/${o.id}/close`, "Close sent.")}>Close</button>}</td></tr>);
          })}</tbody></table>
      )}
      <p className="small muted">Orders sync every 15 s here and every 30 s in the background (crypto trades 24/7). Values are in each market's quote currency.</p>
    </div>
  );
}
