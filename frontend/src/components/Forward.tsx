import { useEffect, useState } from "react";
import { api } from "../api";
import type { Level } from "../App";
import { Replay } from "./Replay";
import { StatusChip, pct } from "./ui";

const STATE = { waiting_entry: "Waiting for next open", open: "Open", closed: "Closed" } as Record<string, string>;

export function Forward({ tick, level, live }: { tick: number; level: Level; live: boolean }) {
  const [d, setD] = useState<any>(null);
  const [showHist, setShowHist] = useState(false);
  useEffect(() => { api.get("/api/forward").then(setD); }, [tick]);
  if (!d) return <p className="muted pad">Loading forward test…</p>;
  const s = d.stats;
  const cur = d.signals.filter((x: any) => x.ruleset_version === d.ruleset_version);
  const older = d.signals.length - cur.length;

  return (
    <div className="forward">
      <p className="muted small">
        {live ? "Live forward test on real completed daily bars." : "Forward test on the demo clock: it grows as you advance sessions."} Rules{" "}
        {d.ruleset_version ? `v${d.ruleset_version}` : "draft (save a version to start recording)"}
        {s.running_since && <> · recording since {s.running_since.slice(0, 10)}</>}
        {!live && <> <span className="simtag">simulated prices</span></>}
      </p>
      <div className="tiles">
        <div><span>Signals recorded</span><b className="num">{s.recorded}</b><small>{s.waiting_entry} waiting · {s.open} open</small></div>
        <div><span>Closed</span><b className="num">{s.closed}</b><small>{s.stops_hit} stopped out</small></div>
        <div><span>Median closed return</span><b className="num">{pct(s.median_return_pct, 2)}</b></div>
        <div><span>Win rate</span><b className="num">{s.win_rate_pct == null ? "—" : `${s.win_rate_pct}%`}</b>{s.win_rate_95ci && <small>95% range {s.win_rate_95ci[0]}–{s.win_rate_95ci[1]}%</small>}</div>
        <div><span>Avg vs buy &amp; hold</span><b className="num">{pct(s.mean_excess_vs_bench_pct, 2)}</b><small>same dates, watchlist</small></div>
      </div>
      {d.sample_warning && <p className="note warn">{d.sample_warning}</p>}
      {level === "beginner" && (
        <p className="verdict small">
          Every time your rules flag a stock on a finished trading day, it's written down here with the evidence at that moment. Then the app
          watches what really happens over your planned holding period. Unlike a backtest, it can't cheat by looking at the future.
        </p>
      )}

      <table className="rules fwd">
        <thead><tr><th>Signal</th><th>Status then</th><th>Entry (next open)</th><th>Stop</th><th>Now / exit</th><th>Return</th><th>vs B&amp;H</th></tr></thead>
        <tbody>
          {cur.length === 0 && <tr><td colSpan={7} className="muted">No signals yet for this rule version.</td></tr>}
          {cur.map((x: any) => (
            <tr key={x.id}>
              <td><b>{x.symbol}</b> <span className="num small muted">{x.signal_date}</span></td>
              <td><StatusChip s={x.status_at_signal} /></td>
              <td className="num small">{x.entry_date ? `${x.entry_date} @ ${x.entry_price.toFixed(2)}` : STATE[x.state]}</td>
              <td className="num small">{x.stop.toFixed(2)}</td>
              <td className="num small">{x.state === "closed" ? `${x.exit_date} @ ${x.exit_price.toFixed(2)} (${x.exit_reason})`
                : x.last_date ? `${x.last_date} close ${x.last_close?.toFixed(2)} · ${STATE[x.state]}` : "—"}</td>
              <td className={`num ${x.return_pct == null ? "" : x.return_pct >= 0 ? "gain" : "loss"}`}>{pct(x.return_pct, 2)}{x.state === "open" ? "*" : ""}</td>
              <td className="num small">{x.bench_return_pct == null ? "—" : pct(x.return_pct - x.bench_return_pct, 2)}</td>
            </tr>
          ))}
        </tbody>
      </table>
      <p className="small muted">* open signals are marked to the latest close. {older > 0 && `${older} signal(s) from earlier rule versions are kept but not counted here.`}</p>

      <h4>Method</h4>
      <ul className="assume small">{Object.entries(d.method).map(([k, v]) => <li key={k}><b>{k}</b>: {String(v)}</li>)}</ul>

      <div className="hist-toggle">
        <button className="btn small ghost" onClick={() => setShowHist(!showHist)}>
          {showHist ? "Hide" : "Show"} historical check (backtest, for context only)
        </button>
      </div>
      {showHist && <div className="hist"><Replay tick={tick} level={level} /></div>}
    </div>
  );
}
