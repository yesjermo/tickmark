import { useCallback, useEffect, useRef, useState } from "react";
import { useDialog } from "./theme";
import { motion } from "motion/react";
import { Check, Copy, ExternalLink, LogIn, RefreshCw, X } from "lucide-react";
import { openMyTradingView, openTradingViewSignIn } from "./TradingView";
import { api, ApiError } from "../api";
import { Monogram } from "./ConnectPortal";

interface Sig { id: number; at: string; symbol: string; action: string; price: number | null; interval: string | null; note: string | null }

/** Connect TradingView: a private alert webhook. Signals arrive as alerts and here; they never place orders. */
export function TradingViewConnect({ notify, onClose, openTicket, symbol = "NVDA" }: { notify: (s: string) => void; onClose: () => void; openTicket: (sym: string) => void; symbol?: string }) {
  const [st, setSt] = useState<{ connected: boolean; created_at: string | null; signals: Sig[]; template: string; layout_url: string | null } | null>(null);
  const [layout, setLayout] = useState("");
  const [url, setUrl] = useState<string | null>(null);
  const load = useCallback(() => api.get<any>("/api/tradingview").then(setSt).catch((e) => notify((e as ApiError).message)), [notify]);
  useEffect(() => { load(); const t = setInterval(load, 15000); return () => clearInterval(t); }, [load]);
  const box = useRef<HTMLElement>(null);
  useDialog(box, onClose);
  async function make() {
    try { const r = await api.post<{ url: string }>("/api/tradingview/token"); setUrl(r.url); load(); }
    catch (e) { notify((e as ApiError).message); }
  }
  async function saveLayout() {
    try {
      const r = await api.put<{ layout_url: string | null }>("/api/tradingview/layout", { url: layout });
      setLayout(""); load(); window.dispatchEvent(new Event("tickmark:tv-layout"));
      notify(r.layout_url ? "Your TradingView layout is saved. \"Open in my TradingView\" uses it." : "Layout link removed.");
    } catch (e) { notify((e as ApiError).message); }
  }
  async function off() {
    try { await api.del("/api/tradingview"); setUrl(null); load(); notify("TradingView disconnected; the old webhook URL no longer works."); }
    catch (e) { notify((e as ApiError).message); }
  }
  const copy = (t: string, what: string) => navigator.clipboard?.writeText(t).then(() => notify(`${what} copied.`), () => notify("Copy failed; select and copy it."));
  return (
    <motion.div className="modal-scrim" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.16 }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <motion.section ref={box} role="dialog" aria-modal="true" aria-label="Connect TradingView" className="portal card"
        initial={{ opacity: 0, y: 16, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 10, scale: 0.98 }}
        transition={{ type: "spring", stiffness: 420, damping: 34 }}>
        <header className="portal-head">
          <Monogram name="Trading View" />
          <div className="grow"><h2>TradingView</h2><p className="small muted">Alerts from your own charts and strategies</p></div>
          <button className="iconbtn" onClick={onClose} aria-label="Close"><X size={16} aria-hidden /></button>
        </header>
        <ol className="portal-steps">
          <li className="now">
            <h3>Your TradingView account</h3>
            <p className="small muted">Sign in on TradingView's own page; it opens in a window next to the desk. Tickmark never sees your password.
              Once you're signed in there, "Open my chart" shows your own TradingView (saved layouts, indicators, drawings) on the symbol you're looking at.</p>
            <div className="row">
              <button className="btn primary" onClick={openTradingViewSignIn}><LogIn size={14} aria-hidden /> Sign in to TradingView</button>
              <button className="btn" onClick={() => openMyTradingView(symbol, "D", st?.layout_url)}><ExternalLink size={14} aria-hidden /> Open my chart ({symbol})</button>
            </div>
            <label className="tv-layout">Your chart layout (optional): in TradingView, open the layout you use and copy its link
              <span className="copyrow">
                <input placeholder={st?.layout_url ?? "https://www.tradingview.com/chart/AbCd1234/"} value={layout} onChange={(e) => setLayout(e.target.value)} spellCheck={false} />
                <button className="btn small" onClick={saveLayout} disabled={!layout.trim()}>Save</button>
                {st?.layout_url && !layout && <button className="btn small ghost" onClick={() => { setLayout(""); api.put("/api/tradingview/layout", { url: "" }).then(() => { load(); window.dispatchEvent(new Event("tickmark:tv-layout")); }); }}>Forget</button>}
              </span>
            </label>
            {st?.layout_url && <p className="small muted"><Check size={12} aria-hidden /> Using your layout {st.layout_url.replace("https://www.tradingview.com", "")}</p>}
          </li>
        </ol>
        <p className="small">Signals from your alerts: TradingView sends them through a private webhook. They arrive here and in the alerts bell
          and never place an order; you review and trade from the ticket.</p>
        <ol className="portal-steps">
          <li className={st?.connected ? "done" : "now"}>
            <h3>{st?.connected ? <><Check size={14} aria-hidden /> Webhook ready</> : "1. Create your private webhook URL"}</h3>
            {url ? (
              <div className="portal-form">
                <label>Webhook URL (shown once; keep it private)
                  <span className="copyrow"><input readOnly value={url} onFocus={(e) => e.target.select()} />
                    <button className="iconbtn" onClick={() => copy(url, "Webhook URL")} aria-label="Copy webhook URL"><Copy size={15} aria-hidden /></button></span>
                </label>
              </div>
            ) : (
              <div className="row">
                <button className="btn primary" onClick={make}>{st?.connected ? <><RefreshCw size={14} aria-hidden /> New URL (old one stops working)</> : "Create webhook URL"}</button>
                {st?.connected && <button className="btn ghost small" onClick={off}>Disconnect</button>}
              </div>
            )}
            {st?.connected && !url && <p className="small muted">Created {new Date(st.created_at!).toLocaleString()}. The URL is only shown when it's created.</p>}
          </li>
          <li className="now">
            <h3>2. In TradingView: Alerts → Create alert → Notifications → Webhook URL</h3>
            <p className="small muted">Paste the URL, and use this as the alert message (TradingView fills in the parts in double braces):</p>
            <span className="copyrow"><code className="tv-template">{st?.template}</code>
              {st && <button className="iconbtn" onClick={() => copy(st.template, "Message template")} aria-label="Copy message template"><Copy size={15} aria-hidden /></button>}</span>
            <p className="small muted">Plain text works too, for example "BUY NVDA". Webhooks need a paid TradingView plan.</p>
          </li>
          <li className="now">
            <h3>3. Signals</h3>
            {!st?.signals.length ? <p className="small muted">No signals yet. Fire a test alert in TradingView and it shows up here within seconds.</p> : (
              <ul className="tv-signals" data-lenis-prevent>
                {st.signals.map((s) => (
                  <li key={s.id}>
                    <span className={`chip act-${s.action}`}>{s.action}</span>
                    <b>{s.symbol}</b>{s.price != null && <span className="num">{s.price}</span>}{s.interval && <span className="muted small">{s.interval}</span>}
                    <span className="muted small grow">{s.note ?? ""}</span>
                    <span className="muted small">{new Date(s.at).toLocaleString([], { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" })}</span>
                    <button className="btn small" onClick={() => { openTicket(s.symbol); onClose(); }}>Open ticket</button>
                  </li>
                ))}
              </ul>
            )}
          </li>
        </ol>
      </motion.section>
    </motion.div>
  );
}
