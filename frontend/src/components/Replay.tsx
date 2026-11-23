import { useEffect, useState } from "react";
import { api } from "../api";
import type { Level } from "../App";
import { pct } from "./ui";

interface Pt { date: string; equity: number }

function Curves({ a, b }: { a: Pt[]; b: Pt[] }) {
  if (!b.length) return null;
  const W = 640, H = 170, P = 28;
  const dates = b.map((p) => p.date);
  const x = (d: string) => P + (dates.indexOf(d) >= 0 ? dates.indexOf(d) : dates.findIndex((z) => z >= d)) / (dates.length - 1) * (W - P * 2);
  const all = [...a.map((p) => p.equity), ...b.map((p) => p.equity), 1];
  const lo = Math.min(...all), hi = Math.max(...all);
  const y = (v: number) => H - P - ((v - lo) / (hi - lo || 1)) * (H - P * 2);
  const strat = [{ date: dates[0], equity: 1 }, ...a];
  const path = (pts: Pt[], step: boolean) => pts.map((p, i) => {
    const px = x(p.date), py = y(p.equity);
    if (i === 0) return `M${px},${py}`;
    return step ? `H${px}V${py}` : `L${px},${py}`;
  }).join("");
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="curves" role="img" aria-label="Strategy equity versus buy-and-hold benchmark">
      <line x1={P} x2={W - P} y1={y(1)} y2={y(1)} className="base" />
      <path d={path(b, false)} className="bench" />
      <path d={path(strat, true)} className="strat" />
      <text x={P} y={14} className="lg"><tspan className="strat-t">━ strategy (marked at exits)</tspan>  <tspan className="bench-t">━ equal-weight buy &amp; hold</tspan></text>
      <text x={P} y={H - 8} className="ax">{dates[0]}</text>
      <text x={W - P} y={H - 8} className="ax" textAnchor="end">{dates[dates.length - 1]}</text>
    </svg>
  );
}

export function Replay({ tick, level }: { tick: number; level: Level }) {
  const [d, setD] = useState<any>(null);
  useEffect(() => { api.get("/api/backtest").then(setD); }, [tick]);
  if (!d) return <p className="muted pad">Running replay…</p>;
  const s = d.stats, b = d.benchmark;
  const beat = s.total_return_pct > b.total_return_pct;
  return (
    <div className="replay">
      <p className="muted small">
        Rules {d.ruleset_version ? `v${d.ruleset_version}` : "draft"} replayed on {d.window.start} → {d.window.end}. {String(d.data_label).startsWith("SIMULATED") ? <span className="simtag">simulated prices</span> : <span className="livetag">real prices; past results, chosen with hindsight</span>}
      </p>
      <div className="tiles">
        <div><span>Trades</span><b className="num">{s.trades}</b></div>
        <div><span>Median trade</span><b className="num">{pct(s.median_return_pct, 2)}</b></div>
        <div><span>Win rate</span><b className="num">{s.win_rate_pct ?? "—"}%</b>{s.win_rate_95ci && <small>95% range {s.win_rate_95ci[0]}–{s.win_rate_95ci[1]}%</small>}</div>
        <div><span>Max drawdown</span><b className="num">{pct(s.max_drawdown_pct, 1)}</b></div>
        <div><span>Strategy total</span><b className="num">{pct(s.total_return_pct, 1)}</b></div>
        <div><span>Buy &amp; hold total</span><b className="num">{pct(b.total_return_pct, 1)}</b><small>max DD {pct(b.max_drawdown_pct, 1)}</small></div>
      </div>
      {d.sample_warning && <p className="note warn">{d.sample_warning}</p>}
      <p className="verdict">
        {level === "beginner"
          ? (beat ? "Over this simulated period, following these rules would have done better than simply holding all four stocks." :
            "Over this simulated period, following these rules would have done worse than simply buying and holding all four stocks. That does not prove the rules are bad, but it is no evidence they help.")
          : `Strategy ${pct(s.total_return_pct, 1)} vs benchmark ${pct(b.total_return_pct, 1)}; ${s.stops_hit} of ${s.trades} trades stopped out. ${beat ? "Outperformed" : "Underperformed"} buy-and-hold over identical dates.`}
      </p>
      <Curves a={d.equity_curve} b={d.benchmark_curve} />
      <h4>Assumptions</h4>
      <ul className="assume small">
        {Object.entries(d.assumptions).map(([k, v]) => <li key={k}><b>{k}</b>: {String(v)}</li>)}
        <li><b>benchmark</b>: {b.definition}</li>
        <li><b>drawdown</b>: {d.drawdown_note}</li>
      </ul>
      <details>
        <summary>All {s.trades} trades</summary>
        <table className="mini">
          <thead><tr><th>Symbol</th><th>Signal</th><th>Entry (open)</th><th>Exit</th><th>Reason</th><th>Return</th></tr></thead>
          <tbody>{d.trades.map((t: any, i: number) => (
            <tr key={i}><td>{t.symbol}</td><td>{t.signal_date}</td><td className="num">{t.entry_date} @ {t.entry.toFixed(2)}</td>
              <td className="num">{t.exit_date} @ {t.exit.toFixed(2)}</td><td>{t.exit_reason}</td>
              <td className={`num ${t.return_pct >= 0 ? "gain" : "loss"}`}>{pct(t.return_pct, 2)}</td></tr>))}</tbody>
        </table>
      </details>
    </div>
  );
}
