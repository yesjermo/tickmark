import { useEffect, useRef, useState } from "react";
import { api, ApiError, Evaluation, Order, RiskCheck, RiskResult, Ruleset, Status_ } from "../api";
import { OrderStatus, money, num } from "./ui";

interface Preview {
  id: string; symbol: string; side: string; qty: number; order_type: string; limit_price: number | null;
  entry_assumption: number; entry_assumption_basis: string; stop_price: number; suggested_stop: number; suggested_qty: number;
  est_position_value: number; pct_of_equity: number; est_loss_if_stopped: number; est_loss_pct_equity: number;
  risk_budget: number; equity: number; sizing: any; fills_at_open_of: string;
  plan: { entry_date: string; exit_date: string; shifted_from?: string; shift_reason?: string };
  earnings_warning: any; ruleset_version: number; data_timestamp: string; mode: string; fill_disclaimer: string;
  evaluation: Evaluation; risk: RiskResult; equity_source: string;
}

const RES_LABEL: Record<string, string> = { pass: "✓", block: "✕ blocked", override: "! needs reason", info: "i" };

function RiskList({ risk, reasons, setReasons }: { risk: RiskResult; reasons: Record<string, string>; setReasons: (r: Record<string, string>) => void }) {
  return (
    <div className="risklist">
      <div className="row between"><b>Pre-trade risk checks</b><span className="muted small">limits v{risk.limits_version}</span></div>
      <ul>
        {risk.checks.map((c: RiskCheck) => (
          <li key={c.id} className={`rk rk-${c.result}`}>
            <span className="rk-res">{RES_LABEL[c.result]}</span>
            <span className="rk-name">{c.name} <em className={`kind ${c.kind}`}>{c.kind}</em></span>
            <span className="num small">{c.observed} <span className="muted">({c.threshold})</span></span>
            {c.detail && c.result !== "pass" && <span className="small muted rk-detail">{c.detail}</span>}
            {c.result === "override" && (
              <input className="rk-reason" placeholder="Reason for overriding (logged, min 10 characters)" value={reasons[c.id] ?? ""}
                onChange={(e) => setReasons({ ...reasons, [c.id]: e.target.value })} />
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

export function OrderTicket({ ev, ruleset, status, onSubmitted, notify, goOrders, prefill, onPrefillUsed }: {
  ev: Evaluation | null; ruleset: Ruleset | null; status: Status_; onSubmitted: () => void; notify: (s: string) => void; goOrders: () => void;
  prefill?: { symbol: string; overrides: Record<string, any>; n: number } | null; onPrefillUsed?: () => void;
}) {
  const [pv, setPv] = useState<Preview | null>(null);
  const [edit, setEdit] = useState({ qty: "", stop_price: "", order_type: "market", limit_price: "" });
  const [thesis, setThesis] = useState("");
  const [ack, setAck] = useState(false);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [liveConfirm, setLiveConfirm] = useState("");
  const real = !!status.live_trading;
  const [stage, setStage] = useState<"idle" | "preview" | "confirm" | "sent">("idle");
  const [err, setErr] = useState<string | null>(null);
  const [order, setOrder] = useState<Order | null>(null);
  const inFlight = useRef(false);

  // Reset when the symbol, the data or the rule version changes, so a stale preview can't be submitted.
  useEffect(() => { setPv(null); setStage("idle"); setErr(null); setOrder(null); setAck(false); setReasons({}); },
    [ev?.symbol, ev?.date, ruleset?.version, status.mode, status.kill_switch?.on]);

  const qualifies = ev && ev.status.startsWith("QUALIFIES");

  // A copilot proposal opens the ticket with its size/stop; the preview is rebuilt here and the user confirms as usual.
  useEffect(() => {
    if (prefill && ev?.symbol === prefill.symbol) { openPreview(prefill.overrides); onPrefillUsed?.(); }
  }, [prefill?.n, ev?.symbol]);

  async function openPreview(overrides: Record<string, any> = {}) {
    if (!ev) return;
    setErr(null);
    try {
      const p = await api.post<Preview>("/api/previews", { symbol: ev.symbol, overrides });
      setPv(p);
      setEdit({ qty: String(p.qty), stop_price: String(p.stop_price), order_type: p.order_type, limit_price: p.limit_price ? String(p.limit_price) : "" });
      if (!thesis) setThesis(`${p.symbol}: quiet pullback to the EMA under rules v${p.ruleset_version}. Exit ${p.plan.exit_date} close or stop ${p.stop_price}.`);
      setStage("preview");
    } catch (e) { setErr((e as ApiError).message); }
  }
  function recalc() {
    const o: Record<string, any> = { qty: Number(edit.qty) || undefined, stop_price: Number(edit.stop_price) || undefined, order_type: edit.order_type };
    if (edit.order_type === "limit") o.limit_price = Number(edit.limit_price) || undefined;
    openPreview(o);
  }
  async function submit() {
    if (!pv || inFlight.current) return;      // client-side guard; the server enforces uniqueness too
    inFlight.current = true;
    setErr(null);
    try {
      const overrides = Object.fromEntries(pv.risk.needs_override.map((id) => [id, reasons[id] ?? ""]));
      const r = await api.post<{ order: Order }>("/api/orders", { preview_id: pv.id, confirm: true, thesis, ack_earnings: ack, risk_overrides: overrides, live_confirm: liveConfirm });
      setOrder(r.order); setStage("sent");
      notify(r.order.status === "rejected" ? `Order rejected: ${r.order.status_reason}` : `Paper order ${r.order.status}: ${r.order.status_reason}`);
      onSubmitted();
    } catch (e) {
      const ae = e as ApiError;
      setErr(ae.message);
      if (ae.body?.risk && pv) { setPv({ ...pv, risk: ae.body.risk }); setStage("preview"); }
      if (ae.body?.order) { setOrder(ae.body.order); setStage("sent"); }
    } finally { inFlight.current = false; }
  }

  if (!ev) return null;
  const dirty = pv && (String(pv.qty) !== edit.qty || String(pv.stop_price) !== edit.stop_price || pv.order_type !== edit.order_type
    || (edit.order_type === "limit" && String(pv.limit_price ?? "") !== edit.limit_price));

  return (
    <div className="ticket">
      <div className={`mode-strip ${real ? "real" : status.mode}`}>
        {real ? "REAL MONEY · ALPACA LIVE — this order uses real funds" : `PAPER · ${status.mode === "simulator" ? "LOCAL SIMULATOR" : "ALPACA PAPER"} — no real money`}
      </div>
      <h3>{ev.symbol} · buy setup</h3>
      {!ruleset?.version && <p className="note warn">Save your rule set first. Every order records the rule version it was placed under.</p>}
      {!qualifies && (
        <p className="note">
          {ev.symbol} does not qualify under {ruleset?.version ? `rules v${ruleset.version}` : "the draft rules"} ({ev.status.replace(/_/g, " ").toLowerCase()}).
          Order previews open only from qualifying setups.
        </p>
      )}
      {qualifies && stage === "idle" && (
        <button className="btn primary" disabled={!ruleset?.version} onClick={() => openPreview()}>Open order preview</button>
      )}
      {err && <p className="err">{err}</p>}

      {pv && stage !== "sent" && (
        <div className="preview">
          <div className="grid2">
            <label>Symbol<input value={pv.symbol} disabled /></label>
            <label>Side<input value="Buy (long)" disabled /></label>
            <label>Quantity<input type="number" value={edit.qty} onChange={(e) => setEdit({ ...edit, qty: e.target.value })} /></label>
            <label>Order type
              <select value={edit.order_type} onChange={(e) => setEdit({ ...edit, order_type: e.target.value })}>
                <option value="market">Market (next open)</option>
                <option value="limit">Limit</option>
              </select>
            </label>
            {edit.order_type === "limit" && (
              <label>Limit price<input type="number" step="0.01" value={edit.limit_price} onChange={(e) => setEdit({ ...edit, limit_price: e.target.value })} /></label>
            )}
            <label>Stop<input type="number" step="0.01" value={edit.stop_price} onChange={(e) => setEdit({ ...edit, stop_price: e.target.value })} /></label>
          </div>
          {dirty && <button className="btn small" onClick={recalc}>Recalculate preview</button>}
          <dl className="figs">
            <dt>Entry assumption</dt><dd className="num">{money(pv.entry_assumption)} <small>{pv.entry_assumption_basis}</small></dd>
            <dt>Stop level</dt><dd className="num">{money(pv.stop_price)} {pv.stop_price !== pv.suggested_stop && <small className="warnc">plan: {money(pv.suggested_stop)}</small>}</dd>
            <dt>Sized from</dt><dd className="small">{pv.equity_source} · equity {money(pv.equity)}</dd>
            <dt>Est. position value</dt><dd className="num">{money(pv.est_position_value)} <small>{num(pv.pct_of_equity, 1)}% of equity</small></dd>
            <dt>Est. loss if filled and stopped</dt><dd className="num loss">{money(pv.est_loss_if_stopped)} <small>{num(pv.est_loss_pct_equity, 2)}% of equity · budget {money(pv.risk_budget)}</small></dd>
            <dt>Fills at</dt><dd className="num">{pv.fills_at_open_of} open <small>the first open that hasn't happened yet</small></dd>
            <dt>Planned exit</dt><dd className="num">{pv.plan.exit_date} close <small>(entry {pv.plan.entry_date}){pv.plan.shifted_from ? ` · window shifted from ${pv.plan.shifted_from}: ${pv.plan.shift_reason}` : ""}</small></dd>
            <dt>Rule version / data</dt><dd className="num small">v{pv.ruleset_version} · {pv.data_timestamp.split(" | ")[0]}</dd>
          </dl>
          <details className="small"><summary>How the size was calculated</summary>
            <p className="muted">{pv.sizing.formula}. Per-share risk {num(pv.sizing.per_share_risk, 2)}; suggested qty {pv.suggested_qty}; {pv.sizing.slippage_assumption_bps_each_side} bps slippage each side included in the loss estimate.</p>
          </details>
          <p className="disclaimer">{pv.fill_disclaimer}</p>
          <RiskList risk={pv.risk} reasons={reasons} setReasons={setReasons} />
          {pv.earnings_warning && (
            <label className="ack"><input type="checkbox" checked={ack} onChange={(e) => setAck(e.target.checked)} />
              I understand earnings ({pv.earnings_warning.observed}) fall inside my holding window.</label>
          )}
          <label className="lbl" htmlFor="thesis">Thesis (saved to the journal, cannot be edited later)</label>
          <textarea id="thesis" rows={3} value={thesis} onChange={(e) => setThesis(e.target.value)} />
          {stage === "preview" && (
            <button className="btn primary" disabled={!!dirty || (pv.earnings_warning && !ack) || pv.risk.blocked
              || pv.risk.needs_override.some((id) => (reasons[id] ?? "").trim().length < 10)} onClick={() => setStage("confirm")}>
              {pv.risk.blocked ? "Blocked by risk checks" : "Review and confirm…"}</button>
          )}
          {stage === "confirm" && (
            <div className="confirm">
              <p>Submit <b>BUY {pv.qty} {pv.symbol}</b> {pv.order_type === "market" ? "at the next open" : `limit ${money(pv.limit_price)}`} with stop <b>{money(pv.stop_price)}</b> to {real ? <b className="loss">your LIVE Alpaca account (real money)</b> : <>the <b>paper</b> {pv.mode === "simulator" ? "simulator" : "Alpaca account"}</>}?</p>
              {real && (
                <label className="liveconfirm">Type <b>{pv.symbol}</b> to send a real-money order
                  <input value={liveConfirm} onChange={(e) => setLiveConfirm(e.target.value.toUpperCase())} aria-label="Type the ticker to confirm" /></label>
              )}
              <div className="row">
                <button className={`btn ${real ? "danger" : "primary"}`} disabled={real && liveConfirm !== pv.symbol} onClick={submit}>
                  {real ? "Send REAL-MONEY order" : "Confirm paper order"}</button>
                <button className="btn ghost" onClick={() => setStage("preview")}>Back</button>
              </div>
            </div>
          )}
        </div>
      )}

      {stage === "sent" && order && (
        <div className="sent">
          <p>Order <code>{order.id}</code> <OrderStatus s={order.status} /></p>
          <p className="small muted">{order.status_reason}</p>
          {order.children?.map((c) => <p key={c.id} className="small">Attached {c.purpose} @ {money(c.stop_price)}: <OrderStatus s={c.status} /> <span className="muted">{c.status_reason}</span></p>)}
          <p className="small">Preview <code>{pv?.id}</code> is now used; submitting it again is blocked.</p>
          <div className="row">
            <button className="btn small" onClick={goOrders}>Track in Orders</button>
            <button className="btn small ghost" disabled title="Duplicate protection: this preview was already submitted" onClick={submit}>Submit again</button>
          </div>
        </div>
      )}
    </div>
  );
}
