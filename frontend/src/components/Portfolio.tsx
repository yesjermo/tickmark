import { useEffect, useMemo, useRef, useState } from "react";
import { motion } from "motion/react";
import { api, ApiError } from "../api";

interface Hist { ts: string; equity: number; cash: number; invested: number; linked_usd: number }
interface Pos { symbol: string; qty: number; last: number; market_value: number; avg_cost: number | null; unrealized: number | null; unrealized_pct: number | null; sector: string | null }
interface P {
  total_usd: number; linked_usd: number; change: number; change_pct: number | null; workspace: string; note: string;
  trading: { equity: number; cash: number; buying_power: number; starting_equity: number };
  positions: Pos[]; allocation: { name: string; kind: string; usd: number; pct: number }[]; history: Hist[];
  realized: { trades: number; closed: number; realized_pnl: number; win_rate_pct: number | null; profit_factor: number | null } | null;
}

const usd = (n: number, d = 2) => n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
const signed = (n: number) => `${n >= 0 ? "+" : "−"}${usd(Math.abs(n))}`;
const WS: Record<string, string> = { backtest: "Backtest account", test: "Paper account (live prices)", trading: "Live account" };

/** Total value over time: one series, area + 2px line, crosshair and tooltip on hover. */
function EquityChart({ hist }: { hist: Hist[] }) {
  const box = useRef<HTMLDivElement>(null);
  const [w, setW] = useState(640);
  const [H, setH] = useState(220);
  const [hover, setHover] = useState<number | null>(null);
  useEffect(() => {
    const el = box.current;
    if (!el) return;
    // The graph takes whatever height the dashboard leaves it, so the whole dashboard fits the window.
    const ro = new ResizeObserver(([e]) => { setW(Math.max(280, e.contentRect.width)); setH(Math.max(140, Math.min(520, e.contentRect.height))); });
    ro.observe(el);
    return () => ro.disconnect();
  }, []);
  const L = 8, R = 64, T = 12, B = 26;
  const pts = hist.map((h) => ({ t: new Date(h.ts).getTime(), v: h.equity + h.linked_usd }));
  const { x, y, ticks, path, area } = useMemo(() => {
    if (!pts.length) return { x: () => 0, y: () => 0, ticks: [] as number[], path: "", area: "" };
    const t0 = pts[0]?.t ?? 0, t1 = pts[pts.length - 1]?.t ?? 1;
    let lo = Math.min(...pts.map((p) => p.v)), hi = Math.max(...pts.map((p) => p.v));
    if (hi - lo < 1) { lo -= 50; hi += 50; }
    const pad = (hi - lo) * 0.12; lo -= pad; hi += pad;
    const x = (t: number) => L + (t1 === t0 ? (w - L - R) / 2 : ((t - t0) / (t1 - t0)) * (w - L - R));
    const y = (v: number) => T + (1 - (v - lo) / (hi - lo)) * (H - T - B);
    const step = (hi - lo) / 3;
    const ticks = [0, 1, 2, 3].map((i) => lo + step * i);
    const path = pts.map((p, i) => `${i ? "L" : "M"}${x(p.t).toFixed(1)},${y(p.v).toFixed(1)}`).join("");
    const area = pts.length > 1 ? `${path}L${x(pts[pts.length - 1].t).toFixed(1)},${H - B}L${x(pts[0].t).toFixed(1)},${H - B}Z` : "";
    return { x, y, ticks, path, area };
  }, [pts, w, H]);
  const hp = hover != null ? pts[hover] : null;
  if (!pts.length) return <div className="eq-chart eq-empty" ref={box}><p className="small muted">No history yet.</p></div>;
  function move(e: React.PointerEvent<SVGSVGElement>) {
    const r = e.currentTarget.getBoundingClientRect();
    const mx = e.clientX - r.left;
    let best = 0;
    pts.forEach((p, i) => { if (Math.abs(x(p.t) - mx) < Math.abs(x(pts[best].t) - mx)) best = i; });
    setHover(best);
  }
  const fmtT = (t: number) => new Date(t).toLocaleString("en-US", { month: "short", day: "numeric", hour: "numeric", minute: "2-digit" });
  return (
    <div className="eq-chart" ref={box}>
      <svg width={w} height={H} role="img" aria-label={`Total value over time, ${pts.length} samples`} onPointerMove={move} onPointerLeave={() => setHover(null)}>
        {ticks.map((v) => (
          <g key={v}>
            <line x1={L} x2={w - R} y1={y(v)} y2={y(v)} className="eq-grid" />
            <text x={w - R + 8} y={y(v) + 4} className="eq-axis">{usd(v, 0)}</text>
          </g>
        ))}
        {pts.length > 1 && <text x={L} y={H - 6} className="eq-axis">{fmtT(pts[0].t)}</text>}
        {pts.length > 1 && <text x={w - R} y={H - 6} textAnchor="end" className="eq-axis">{fmtT(pts[pts.length - 1].t)}</text>}
        {area && <path d={area} className="eq-area" />}
        <motion.path d={path} className="eq-line" initial={{ pathLength: 0 }} animate={{ pathLength: 1 }} transition={{ duration: 0.6, ease: [0.2, 0.8, 0.2, 1] }} />
        {pts.length === 1 && <circle cx={x(pts[0].t)} cy={y(pts[0].v)} r={5} className="eq-dot" />}
        {hp && <>
          <line x1={x(hp.t)} x2={x(hp.t)} y1={T} y2={H - B} className="eq-cross" />
          <circle cx={x(hp.t)} cy={y(hp.v)} r={5} className="eq-dot" />
        </>}
      </svg>
      {hp && (
        <div className="eq-tip" style={{ left: Math.min(Math.max(x(hp.t), 70), w - 140), top: y(hp.v) - 54 < 2 ? y(hp.v) + 12 : y(hp.v) - 54 }}>
          <b className="num">{usd(hp.v)}</b><span>{fmtT(hp.t)}</span>
        </div>
      )}
    </div>
  );
}

export function Portfolio({ tick, notify }: { tick: number; notify: (s: string) => void }) {
  const [p, setP] = useState<P | null>(null);
  useEffect(() => { api.get<P>("/api/portfolio").then(setP).catch((e) => notify((e as ApiError).message)); }, [tick, notify]);
  if (!p) return <div className="pad muted">Loading the portfolio…</div>;
  const maxAlloc = Math.max(...p.allocation.map((a) => a.usd), 1);
  const unreal = p.positions.reduce((s, x) => s + (x.unrealized ?? 0), 0);
  return (
    <div className="portfolio">
      <section className="pf-hero">
        <div>
          <span className="pf-k">Total balance · {WS[p.workspace] ?? "Account"}{p.linked_usd ? " + linked accounts" : ""}</span>
          <div className="pf-v num">{usd(p.total_usd)}<span>USD</span></div>
          <span className={`pf-d num ${p.change > 0 ? "gain" : p.change < 0 ? "loss" : ""}`}>
            {signed(p.change)}{p.change_pct != null && ` (${p.change_pct >= 0 ? "+" : "−"}${Math.abs(p.change_pct).toFixed(2)}%)`}<em> since start</em></span>
        </div>
        <dl className="pf-stats">
          <div><dt>Cash</dt><dd className="num">{usd(p.trading.cash)}</dd></div>
          <div><dt>Buying power</dt><dd className="num">{usd(p.trading.buying_power)}</dd></div>
          <div><dt>Unrealized P&amp;L</dt><dd className={`num ${unreal > 0 ? "gain" : unreal < 0 ? "loss" : ""}`}>{signed(unreal)}</dd></div>
          <div><dt>Realized P&amp;L</dt><dd className="num">{p.realized ? signed(p.realized.realized_pnl) : "—"}</dd></div>
          <div><dt>Win rate</dt><dd className="num">{p.realized?.win_rate_pct != null ? `${p.realized.win_rate_pct}%` : "—"}</dd></div>
          <div><dt>Closed trades</dt><dd className="num">{p.realized?.closed ?? 0}</dd></div>
        </dl>
      </section>

      <section className="pf-block pf-graph">
        <h3>Total value over time</h3>
        <EquityChart hist={p.history} />
        {p.history.length < 3 && <p className="small muted">The graph fills in as you use the desk: the value is recorded at most every 15 minutes.</p>}
      </section>

      <div className="pf-grid">
        <section className="pf-block" data-lenis-prevent>
          <h3>Allocation</h3>
          <ul className="alloc">
            {p.allocation.map((a, i) => (
              <li key={a.name} title={`${a.name}: ${usd(a.usd)} (${a.pct}%)`}>
                <span className="alloc-name">{a.name}<small>{a.kind === "cash" ? "cash" : a.kind === "linked" ? "linked account" : "stock"}</small></span>
                <span className="alloc-track"><motion.span className="alloc-bar" initial={{ width: 0 }} animate={{ width: `${(a.usd / maxAlloc) * 100}%` }}
                  transition={{ duration: 0.45, delay: i * 0.04, ease: [0.2, 0.8, 0.2, 1] }} /></span>
                <span className="alloc-v num">{a.pct.toFixed(1)}%<small>{usd(a.usd, 0)}</small></span>
              </li>
            ))}
          </ul>
        </section>
        <section className="pf-block pf-positions" data-lenis-prevent>
          <h3>Positions</h3>
          {p.positions.length === 0 ? <p className="small muted">No open positions. Paper trades you place show up here with their cost and P&amp;L.</p> : (
            <table className="pf-table">
              <thead><tr><th>Symbol</th><th className="r">Qty</th><th className="r">Avg cost</th><th className="r">Last</th><th className="r">Value</th><th className="r">P&amp;L</th></tr></thead>
              <tbody>
                {p.positions.map((x) => (
                  <tr key={x.symbol}>
                    <td><b>{x.symbol}</b>{x.sector && <small className="muted"> {x.sector}</small>}</td>
                    <td className="r num">{x.qty}</td>
                    <td className="r num">{x.avg_cost != null ? usd(x.avg_cost) : "—"}</td>
                    <td className="r num">{usd(x.last)}</td>
                    <td className="r num">{usd(x.market_value)}</td>
                    <td className={`r num ${(x.unrealized ?? 0) > 0 ? "gain" : (x.unrealized ?? 0) < 0 ? "loss" : ""}`}>
                      {x.unrealized != null ? `${signed(x.unrealized)} (${x.unrealized_pct}%)` : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}
        </section>
      </div>
      <p className="small muted">{p.note}</p>
    </div>
  );
}
