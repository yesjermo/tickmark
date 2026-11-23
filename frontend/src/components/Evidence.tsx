import { useEffect, useState } from "react";
import { api, Evaluation, Fact, Ruleset } from "../api";
import type { Level } from "../App";
import { ResultBadge, StatusChip, num } from "./ui";

interface Explanation {
  level: string; status: string; title: string; summary: string; narrator: string; llm_note: string | null; narrative?: string;
  lines: { text: string; rule: string; result: any; facts: string[] }[];
}

/** Clickable number: opens the inputs behind a computed value. */
export function Num({ children, fact, onOpen }: { children: React.ReactNode; fact: string; onOpen: (f: string) => void }) {
  return <button className="numlink" onClick={() => onOpen(fact)} title="Show the inputs behind this number">{children}</button>;
}

export function InputsDrawer({ ev, factId, onClose }: { ev: Evaluation; factId: string | null; onClose: () => void }) {
  if (!factId) return null;
  const f: Fact = ev.facts[factId];
  return (
    <div className="drawer" role="dialog" aria-label={`Inputs for ${f.label}`}>
      <div className="drawer-head">
        <b>{f.label}</b>
        <button className="btn small ghost" onClick={onClose}>Close</button>
      </div>
      <div className="kv">
        <span>Value</span><code>{typeof f.value === "object" ? JSON.stringify(f.value) : String(f.value)}</code>
      </div>
      {f.inputs && Object.entries(f.inputs).map(([k, v]) =>
        k === "sessions" && Array.isArray(v) ? (
          <details key={k} open>
            <summary>{v.length} prior sessions used (signal session excluded)</summary>
            <table className="mini">
              <thead><tr><th>Date</th><th>Volume</th></tr></thead>
              <tbody>{v.map((r: any) => <tr key={r.date}><td>{r.date}</td><td className="num">{r.volume?.toLocaleString() ?? "missing"}</td></tr>)}</tbody>
            </table>
          </details>
        ) : (
          <div className="kv" key={k}><span>{k.replace(/_/g, " ")}</span><code>{Array.isArray(v) ? v.join(", ") : typeof v === "object" && v ? JSON.stringify(v) : String(v)}</code></div>
        ))}
      {f.source && (
        <div className="kv"><span>source</span><code>{Object.entries(f.source).map(([k, v]) => `${k}: ${v}`).join(" · ")}</code></div>
      )}
      <p className="small muted">Computed in Python (backend/app/rules.py). The explanation text is generated from these values and cannot change them.</p>
    </div>
  );
}

function fmtFact(key: string, v: any): string {
  if (v == null) return "n/a";
  if (typeof v !== "number") return String(v);
  if (key === "volume_ratio") return `${v.toFixed(2)}x avg`;
  if (key === "range_position") return `${Math.round(v * 100)}% of range`;
  if (key === "gap") return `${v >= 0 ? "+" : ""}${v.toFixed(2)}%`;
  if (key === "rsi") return v.toFixed(1);
  return v.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

export function Evidence({ ev, level, ruleset, tick }: { ev: Evaluation; level: Level; ruleset: Ruleset; tick: number }) {
  const [ex, setEx] = useState<Explanation | null>(null);
  const [open, setOpen] = useState<string | null>(null);
  useEffect(() => {
    api.get<{ explanation: Explanation }>(`/api/symbols/${ev.symbol}/explain?level=${level}`).then((r) => setEx(r.explanation));
  }, [ev.symbol, level, tick]);
  useEffect(() => setOpen(null), [ev.symbol]);

  const f = ev.facts;
  const bar = f.bar.value;
  return (
    <div className="evidence">
      <div className="ev-head">
        <div>
          <h2>{ev.symbol} <StatusChip s={ev.status} /></h2>
          <p className="muted small">
            {ev.strategy_name ?? "EMA pullback"} · rules {ruleset.version ? `v${ruleset.version}` : "draft (unconfirmed)"} · signal bar {ev.date} · planned hold {ev.plan.entry_date} → {ev.plan.exit_date}
          </p>
        </div>
        {ex && <p className="ev-summary"><b>{ex.title}.</b> {ex.summary}</p>}
      </div>

      {level === "beginner" && ex && (
        <ul className="plain">
          {ex.lines.map((l, i) => (
            <li key={i} className={`pl-${l.result}`}>
              <ResultBadge r={l.result} />
              <span>{l.text}</span>
            </li>
          ))}
          {ex.narrative && <li className="pl-info"><span className="small">{ex.narrative}</span></li>}
        </ul>
      )}

      {level === "analyst" && (
        <div className="stats-row">
          <div><span>Candle {bar.date}</span>
            <b className="num">O {num(bar.open)} H {num(bar.high)} <Num fact="bar" onOpen={setOpen}>L {num(bar.low)}</Num> <Num fact="bar" onOpen={setOpen}>C {num(bar.close)}</Num></b></div>
          {Object.entries(f).filter(([k]) => !["bar", "plan", "earnings", "avg_volume"].includes(k)).map(([k, fact]) => (
            <div key={k}><span>{fact.label}</span><b className="num"><Num fact={k} onOpen={setOpen}>{fmtFact(k, fact.value)}</Num></b></div>
          ))}
          <div><span>Next earnings</span><b className="num"><Num fact="earnings" onOpen={setOpen}>{f.earnings.value ?? "unknown"}</Num></b></div>
          <div><span>Planned stop</span><b className="num">{ev.suggested_stop ?? "n/a"}</b></div>
          <div className="wide"><span>Data timestamp</span><b className="num small">{ev.data_timestamp}</b></div>
        </div>
      )}

      <table className="rules">
        <thead><tr><th>Rule</th><th>Threshold</th><th>Observed</th><th>Result</th>{level === "analyst" && <th>Margin</th>}</tr></thead>
        <tbody>
          {ev.rules.map((r) => (
            <tr key={r.id} className={`row-${r.result}`}>
              <td>{r.name}</td>
              <td className="num small">{r.threshold}</td>
              <td className="num"><Num fact={r.facts[r.facts.length - 1]} onOpen={setOpen}>{r.observed}</Num></td>
              <td><ResultBadge r={r.result} /></td>
              {level === "analyst" && <td className="num small muted">{r.margin ?? ""}</td>}
            </tr>
          ))}
        </tbody>
      </table>
      <p className="small muted">
        Same numbers and verdict in both views; only the wording changes. Click any value to see its inputs. Explanations: {ex?.narrator ?? "…"}.
        {ex?.llm_note && <> {ex.llm_note}</>}
      </p>
      <InputsDrawer ev={ev} factId={open} onClose={() => setOpen(null)} />
    </div>
  );
}
