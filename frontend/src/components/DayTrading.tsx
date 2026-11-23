import { useEffect, useRef, useState } from "react";
import { createChart, IChartApi, ISeriesApi, SeriesMarker, UTCTimestamp } from "lightweight-charts";
import { api, ApiError } from "../api";
import { ResultBadge, StatusChip, money } from "./ui";

interface DayState {
  mode: "live" | "replay"; date: string; cursor: string; market_open: boolean; feed: string; version: number; data_mode: string;
  params: Record<string, any>; strategies: Record<string, { name: string; style: string }>;
}
interface DayEval {
  symbol: string; status: any; rules: { id: string; name: string; threshold: string; observed: string; result: any; margin: string | null }[];
  facts: Record<string, { label: string; value: any }>; suggested_stop: number | null; target: number | null; entry_assumption: number | null;
  strategy_name: string;
}
interface Pos { id: string; symbol: string; status: string; broker: string; qty: number; held: number; entry: number | null; stop: number | null; target: number | null;
  last: number | null; open_pnl: number | null; closed: any }

// Bars are ET wall-clock strings; show them on the chart as-is by treating them as UTC.
const ts = (s: string) => (Date.parse(s + ":00Z") / 1000) as UTCTimestamp;
const css = (n: string) => getComputedStyle(document.documentElement).getPropertyValue(n).trim();

function lastSession(): string {
  // Local calendar date (toISOString would convert to UTC and can land on a weekend).
  const d = new Date(); d.setDate(d.getDate() - 1);
  while (d.getDay() === 0 || d.getDay() === 6) d.setDate(d.getDate() - 1);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
}

function DayChart({ symbol, tick }: { symbol: string; tick: number }) {
  const el = useRef<HTMLDivElement>(null);
  const ref = useRef<{ chart: IChartApi; c: ISeriesApi<"Candlestick">; vw: ISeriesApi<"Line">; v: ISeriesApi<"Histogram">; lines: any[] } | null>(null);
  useEffect(() => {
    if (!el.current) return;
    const chart = createChart(el.current, {
      autoSize: true, layout: { background: { color: "transparent" }, textColor: css("--muted"), fontFamily: "IBM Plex Mono, ui-monospace, monospace", fontSize: 11 },
      grid: { vertLines: { color: css("--grid") }, horzLines: { color: css("--grid") } },
      timeScale: { timeVisible: true, secondsVisible: false, borderColor: css("--line") }, rightPriceScale: { borderColor: css("--line") },
    });
    const c = chart.addCandlestickSeries({ upColor: css("--up"), downColor: css("--down"), borderVisible: false, wickUpColor: css("--up"), wickDownColor: css("--down") });
    const vw = chart.addLineSeries({ color: css("--accent"), lineWidth: 2, priceLineVisible: false, title: "VWAP" });
    const v = chart.addHistogramSeries({ priceScaleId: "vol", priceFormat: { type: "volume" }, color: css("--vol") });
    chart.priceScale("vol").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
    c.priceScale().applyOptions({ scaleMargins: { top: 0.06, bottom: 0.22 } });
    ref.current = { chart, c, vw, v, lines: [] };
    return () => { ref.current = null; requestAnimationFrame(() => chart.remove()); };
  }, []);
  useEffect(() => {
    let off = false;
    api.get<any>(`/api/day/chart/${symbol}`).then((d) => {
      const r = ref.current;
      if (off || !r) return;
      r.c.setData(d.bars.map((b: any) => ({ time: ts(b.ts), open: b.open, high: b.high, low: b.low, close: b.close })));
      r.v.setData(d.bars.map((b: any) => ({ time: ts(b.ts), value: b.volume, color: css("--vol") })));
      r.vw.setData(d.bars.map((b: any, i: number) => ({ time: ts(b.ts), value: d.vwap[i] })).filter((x: any) => x.value != null));
      r.lines.forEach((l) => r.c.removePriceLine(l));
      r.lines = d.opening_range ? [
        r.c.createPriceLine({ price: d.opening_range.high, color: css("--warn"), lineStyle: 2, lineWidth: 1, title: "OR high", axisLabelVisible: true }),
        r.c.createPriceLine({ price: d.opening_range.low, color: css("--warn"), lineStyle: 2, lineWidth: 1, title: "OR low", axisLabelVisible: true }),
      ] : [];
      const markers: SeriesMarker<UTCTimestamp>[] = d.fills.map((f: any) => {
        const t = f.note?.match(/(\d\d:\d\d)/)?.[1];
        const bar = d.bars.find((b: any) => b.ts.slice(11, 16) === t) ?? d.bars[d.bars.length - 1];
        return { time: ts(bar.ts), position: f.side === "buy" ? "belowBar" : "aboveBar", shape: f.side === "buy" ? "arrowUp" : "arrowDown",
          color: f.side === "buy" ? css("--accent") : css("--text"), text: `${f.side} ${f.qty} @ ${Number(f.price).toFixed(2)}` };
      }).filter((m: any) => m.time);
      r.c.setMarkers(markers.sort((a, b) => a.time - b.time));
      r.chart.timeScale().fitContent();
    }).catch(() => {});
    return () => { off = true; };
  }, [symbol, tick]);
  return <div ref={el} className="daychart" />;
}

export function DayTrading({ notify, onChanged }: { notify: (s: string) => void; onChanged: () => void }) {
  const [st, setSt] = useState<DayState | null>(null);
  const [rows, setRows] = useState<DayEval[]>([]);
  const [sel, setSel] = useState("NVDA");
  const [pos, setPos] = useState<Pos[]>([]);
  const [pv, setPv] = useState<any>(null);
  const [reasons, setReasons] = useState<Record<string, string>>({});
  const [replayDate, setReplayDate] = useState(lastSession());
  const [playing, setPlaying] = useState(false);
  const [tick, setTick] = useState(0);
  const [busy, setBusy] = useState(false);
  const sending = useRef(false);

  const load = async () => {
    try {
      const [s, sc, p] = await Promise.all([api.get<DayState>("/api/day/state"), api.get<any>("/api/day/screen"), api.get<Pos[]>("/api/day/positions")]);
      setSt(s); setRows(sc.results); setPos(p);
      if (sc.error) notify(`Live intraday data: ${sc.error}`);
      setTick((t) => t + 1);
    } catch (e) { notify((e as ApiError).message); }
  };
  useEffect(() => { load(); }, []);
  useEffect(() => {           // live mode follows the clock
    if (st?.mode !== "live" || !st.market_open) return;
    const t = setInterval(load, 30000);
    return () => clearInterval(t);
  }, [st?.mode, st?.market_open]);
  useEffect(() => {           // replay autoplay: one bar per second
    if (!playing) return;
    const t = setInterval(async () => {
      try { const r = await api.post<any>("/api/day/step", { bars: 1 }); r.events?.forEach((e: string) => notify(e)); setPv(null); await load();
        if (r.cursor.endsWith("16:00")) setPlaying(false); } catch (e) { setPlaying(false); notify((e as ApiError).message); }
    }, 1000);
    return () => clearInterval(t);
  }, [playing]);

  if (st && st.data_mode !== "live") return <p className="pad note warn">Day trading uses real intraday prices. Switch to <b>Live data</b> in the top bar first.</p>;
  if (!st) return <p className="pad muted">Loading the trading day…</p>;

  async function go(fn: () => Promise<any>, after?: (r: any) => void) {
    setBusy(true);
    try { const r = await fn(); after?.(r); setPv(null); await load(); onChanged(); } catch (e) { notify((e as ApiError).message); } finally { setBusy(false); }
  }
  const step = (n: number) => go(() => api.post("/api/day/step", { bars: n }), (r) => r.events?.length && notify(r.events.join(" · ")));
  const replay = () => go(() => api.put("/api/day/session", { mode: "replay", date: replayDate }), () => notify(`Replaying ${replayDate}: only bars up to the cursor are visible.`));
  const goLive = () => go(() => api.put("/api/day/session", { mode: "live" }));
  const saveRules = (patch: Record<string, any>) => go(() => api.put("/api/day/rules", { params: { ...st.params, ...patch } }), (r) => notify(`Day-trading rules saved as v${r.version}.`));
  const preview = () => go(() => api.post("/api/day/preview", { symbol: sel }), (r) => setTimeout(() => setPv(r), 0));
  async function submit() {
    if (!pv || sending.current) return;
    sending.current = true;
    try {
      await api.post("/api/day/orders", { preview_id: pv.id, confirm: true, risk_overrides: Object.fromEntries(pv.risk.needs_override.map((id: string) => [id, reasons[id] ?? ""])) });
      notify(`Day trade placed: buy ${pv.qty} ${pv.symbol}, fills at the next bar open.`); setPv(null); await load(); onChanged();
    } catch (e) { notify((e as ApiError).message); } finally { sending.current = false; }
  }
  const ev = rows.find((r) => r.symbol === sel);
  const p = st.params;

  return (
    <div className="day">
      <div className="daybar">
        <div className="seg">
          <button className={st.mode === "live" ? "on" : ""} onClick={goLive} disabled={busy}>Live session</button>
          <button className={st.mode === "replay" ? "on" : ""} onClick={replay} disabled={busy}>Replay a past day</button>
        </div>
        <input type="date" value={replayDate} onChange={(e) => setReplayDate(e.target.value)} aria-label="Replay date" />
        {st.mode === "replay" ? (
          <>
            <button className="btn small" disabled={busy || playing} onClick={() => step(1)}>Next bar ▸</button>
            <button className="btn small" disabled={busy || playing} onClick={() => step(6)}>+30 min ▸▸</button>
            <button className="btn small" onClick={() => setPlaying(!playing)}>{playing ? "Pause ❚❚" : "Play ▶"}</button>
          </>
        ) : <span className={`small ${st.market_open ? "gain" : "muted"}`}>{st.market_open ? "Market open · refreshes every 30 s" : "Market closed"}</span>}
        <span className="num"><b>{st.date}</b> {st.cursor.slice(11)} ET</span>
        <span className="muted small">{st.mode === "replay" ? "replay" : "live"} · {st.feed === "yahoo" ? "Yahoo 5-min" : st.feed === "alpaca:sip" ? "Alpaca SIP 5-min" : "Alpaca IEX real-time 5-min"}</span>
      </div>

      <div className="dayrules">
        <select value={p.strategy} onChange={(e) => saveRules({ strategy: e.target.value })} aria-label="Day-trading strategy">
          {Object.entries(st.strategies).map(([k, v]) => <option key={k} value={k}>{v.name}</option>)}
        </select>
        <label>RVOL ≥ <input type="number" step={0.1} defaultValue={p.rvol_min} onBlur={(e) => Number(e.target.value) !== p.rvol_min && saveRules({ rvol_min: Number(e.target.value) })} /></label>
        <label>Window <input defaultValue={p.window_start} onBlur={(e) => e.target.value !== p.window_start && saveRules({ window_start: e.target.value })} />–<input defaultValue={p.window_end} onBlur={(e) => e.target.value !== p.window_end && saveRules({ window_end: e.target.value })} /></label>
        <label>Target <input type="number" step={0.5} defaultValue={p.target_r} onBlur={(e) => Number(e.target.value) !== p.target_r && saveRules({ target_r: Number(e.target.value) })} /> R</label>
        <label>Flat by <input defaultValue={p.flat_by} onBlur={(e) => e.target.value !== p.flat_by && saveRules({ flat_by: e.target.value })} /></label>
        {st.version ? <span className="muted small">rules v{st.version}</span>
          : <button className="btn small primary" onClick={() => saveRules({})}>Save these rules as v1</button>}
      </div>

      <div className="daygrid">
        <div className="card daychart-card">
          <div className="chart-head"><b>{sel}</b><span className="muted small">5-min · VWAP · opening range</span></div>
          <DayChart symbol={sel} tick={tick} />
        </div>
        <div className="dayside">
          <table className="rules dayscreen">
            <thead><tr><th>Symbol</th><th>Where</th><th>Status</th><th>Last</th><th>VWAP</th><th>RVOL</th></tr></thead>
            <tbody>{rows.map((r) => (
              <tr key={r.symbol} className={r.symbol === sel ? "selrow" : ""} onClick={() => { setSel(r.symbol); setPv(null); }}>
                <td><b>{r.symbol}</b></td><td><StatusChip s={r.status} /></td>
                <td className="num">{r.facts.bar?.value?.close?.toFixed(2) ?? "—"}</td>
                <td className="num">{r.facts.vwap?.value?.toFixed(2) ?? "—"}</td>
                <td className="num">{r.facts.rvol?.value != null ? `${r.facts.rvol.value.toFixed(2)}x` : "—"}</td>
              </tr>))}</tbody>
          </table>
          {ev && (
            <ul className="plain dayrules-list">
              {ev.rules.map((r) => (
                <li key={r.id} className={`pl-${r.result}`}><ResultBadge r={r.result === "info" ? "info" : r.result} />
                  <span><b>{r.name}</b> <span className="muted small">{r.observed} ({r.threshold})</span></span></li>
              ))}
            </ul>
          )}
          {ev && ev.status === "QUALIFIES" && !pv && <button className="btn primary" disabled={busy || !st.version} onClick={preview}>Preview day trade</button>}
          {pv && (
            <div className="card dayticket">
              {pv.route?.broker && pv.route.broker !== "simulator"
                ? <div className={`mode-strip ${pv.route.broker}`}>PAPER · {pv.route.label.toUpperCase()} · DAY TRADE (BRACKET)</div>
                : <div className="mode-strip simulator">PAPER · LOCAL SIMULATOR · DAY TRADE</div>}
              {pv.route && <p className={`small ${pv.route.blocked ? "loss" : "muted"}`}>{pv.route.why}</p>}
              <dl className="figs">
                <dt>Buy</dt><dd className="num">{pv.qty} {pv.symbol} {pv.route?.broker && pv.route.broker !== "simulator" ? "at market" : "at the next bar open"} (~{money(pv.entry_assumption)})</dd>
                <dt>Stop / target</dt><dd className="num">{money(pv.stop_price)} / {money(pv.target)}</dd>
                <dt>Flat by</dt><dd className="num">{pv.flat_by} ET</dd>
                <dt>Est. loss if stopped</dt><dd className="num loss">{money(pv.est_loss_if_stopped)} <small>budget {money(pv.risk_budget)}</small></dd>
              </dl>
              <ul className="risklist-mini">{pv.risk.checks.map((c: any) => (
                <li key={c.id} className={`rk-${c.result}`}>{c.result === "pass" ? "✓" : c.result === "block" ? "✕" : c.result === "override" ? "!" : "i"} {c.name}
                  <span className="muted small"> {c.observed}</span>
                  {c.result === "override" && <input placeholder="Reason for overriding (min 10 chars)" value={reasons[c.id] ?? ""} onChange={(e) => setReasons({ ...reasons, [c.id]: e.target.value })} />}
                </li>))}</ul>
              <p className="disclaimer">{pv.fill_disclaimer}</p>
              <div className="row">
                <button className="btn primary" disabled={pv.route?.blocked || pv.risk.blocked || pv.risk.needs_override.some((id: string) => (reasons[id] ?? "").trim().length < 10)} onClick={submit}>
                  {pv.route?.blocked ? "Can't send" : pv.risk.blocked ? "Blocked by risk checks"
                    : pv.route?.broker && pv.route.broker !== "simulator" ? `Send to ${pv.route.label} paper` : "Confirm paper day trade"}</button>
                <button className="btn ghost" onClick={() => setPv(null)}>Cancel</button>
              </div>
            </div>
          )}
          <h4>Day trades</h4>
          {pos.length === 0 ? <p className="small muted">None yet.</p> : (
            <table className="mini"><thead><tr><th>Symbol</th><th>Where</th><th>Status</th><th>Entry</th><th>Stop / target</th><th>P&amp;L</th><th></th></tr></thead>
              <tbody>{pos.map((x) => (
                <tr key={x.id}><td>{x.symbol}</td><td className="small muted">{x.broker === "simulator" ? "sim" : x.broker === "alpaca_paper" ? "Alpaca" : x.broker}</td><td>{x.closed ? x.closed.exit_reason : x.held ? "open" : x.status}</td>
                  <td className="num">{x.entry?.toFixed(2) ?? "—"}</td><td className="num">{x.stop} / {x.target}</td>
                  <td className={`num ${(x.closed?.realized_pnl ?? x.open_pnl ?? 0) >= 0 ? "gain" : "loss"}`}>{x.closed ? money(x.closed.realized_pnl) : x.open_pnl != null ? money(x.open_pnl) : "—"}</td>
                  <td>{x.held > 0 && <button className="btn small ghost" onClick={() => go(() => api.post(`/api/day/orders/${x.id}/flatten`), () => notify(x.broker === "simulator" ? "Flatten queued for the next bar open." : "Market sell sent to the broker (bracket legs canceled first)."))}>Flatten</button>}</td></tr>))}</tbody></table>
          )}
        </div>
      </div>
    </div>
  );
}
