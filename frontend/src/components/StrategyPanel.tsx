import { StrategyResearch } from "./StrategyResearch";
import { useEffect, useState } from "react";
import { api, ApiError, Interpretation, Params, Ruleset, StrategyInfo } from "../api";

type Field = { key: keyof Params; label: string; step: number; scale?: number; suffix: string; help: string };
const F: Record<string, Field> = {
  ema_period: { key: "ema_period", label: "EMA period", step: 1, suffix: "days", help: "Length of the exponential moving average." },
  volume_ratio_max: { key: "volume_ratio_max", label: "Lighter volume below", step: 1, scale: 100, suffix: "% of avg", help: "Signal session volume must be below this share of the prior average." },
  volume_lookback: { key: "volume_lookback", label: "Volume average over", step: 1, suffix: "prior sessions", help: "Signal session itself is excluded." },
  breakout_lookback: { key: "breakout_lookback", label: "Breakout above the high of", step: 1, suffix: "prior sessions", help: "Highest high of these sessions, not counting the signal day." },
  volume_ratio_min: { key: "volume_ratio_min", label: "Volume at least", step: 0.1, suffix: "x average", help: "Signal session volume as a multiple of the prior average." },
  close_range_min: { key: "close_range_min", label: "Close at least", step: 5, scale: 100, suffix: "% up the day's range", help: "(close - low) / (high - low)." },
  rsi_period: { key: "rsi_period", label: "RSI period", step: 1, suffix: "sessions", help: "Wilder RSI; short periods (2-3) catch quick washouts." },
  rsi_max: { key: "rsi_max", label: "Oversold below RSI", step: 1, suffix: "", help: "Lower is more extreme." },
  trend_sma: { key: "trend_sma", label: "Long-term trend average", step: 10, suffix: "days (SMA)", help: "Close must be above this simple moving average." },
  fast_ema: { key: "fast_ema", label: "Fast EMA", step: 1, suffix: "days", help: "Crosses above the slow average to signal." },
  slow_sma: { key: "slow_sma", label: "Slow SMA", step: 5, suffix: "days", help: "Must be rising." },
  slope_lookback: { key: "slope_lookback", label: "Slow SMA rising over", step: 1, suffix: "sessions", help: "SMA today vs this many sessions ago." },
  atr_period: { key: "atr_period", label: "ATR period", step: 1, suffix: "sessions", help: "Average true range, Wilder." },
  atr_mult: { key: "atr_mult", label: "Stop distance", step: 0.5, suffix: "x ATR below close", help: "Wider stops for more volatile stocks." },
  gap_min_pct: { key: "gap_min_pct", label: "Gap up at least", step: 0.5, suffix: "% vs prev close", help: "Signal session open vs the previous close." },
  hold_sessions: { key: "hold_sessions", label: "Planned holding period", step: 1, suffix: "sessions", help: "Entry day counts as session 1; exit at that session's close." },
  stop_buffer_pct: { key: "stop_buffer_pct", label: "Stop buffer", step: 0.1, suffix: "% below the stop level", help: "Extra room below the strategy's stop level." },
  risk_per_trade_pct: { key: "risk_per_trade_pct", label: "Risk per trade", step: 0.05, suffix: "% of equity", help: "Loss if stopped at the planned price." },
  max_position_pct: { key: "max_position_pct", label: "Max position", step: 1, suffix: "% of equity", help: "Caps position value." },
};
const SHARED = ["hold_sessions", "stop_buffer_pct", "risk_per_trade_pct", "max_position_pct"];

export function StrategyPanel({ onSaved, notify }: { onSaved: () => void; notify?: (s: string) => void }) {
  const [text, setText] = useState("");
  const [draft, setDraft] = useState<{ params: Params; interpretations: Interpretation[] } | null>(null);
  const [active, setActive] = useState<Ruleset | null>(null);
  const [history, setHistory] = useState<Ruleset[]>([]);
  const [err, setErr] = useState<string | null>(null);
  const [strategies, setStrategies] = useState<StrategyInfo[]>([]);
  useEffect(() => { api.get<StrategyInfo[]>("/api/strategies").then(setStrategies); }, []);

  const load = () => api.get<{ active: Ruleset | null; draft: Ruleset; history: Ruleset[] }>("/api/rulesets").then((r) => {
    const base = r.active ?? r.draft;
    setActive(r.active); setHistory(r.history);
    setText(base.source_text); setDraft({ params: base.params, interpretations: base.interpretations });
  });
  useEffect(() => { load(); }, []);

  async function translate(t: string = text) {
    setErr(null);
    try {
      const r = await api.post<Ruleset>("/api/strategy/parse", { text: t });
      setDraft({ params: r.params, interpretations: r.interpretations });
    } catch (e) { setErr((e as ApiError).message); }
  }
  async function save() {
    if (!draft) return;
    setErr(null);
    try {
      await api.post("/api/rulesets", { text, params: draft.params, interpretations: draft.interpretations });
      await load();
      onSaved();
    } catch (e) { setErr((e as ApiError).message); }
  }
  const set = (k: keyof Params, v: any) => draft && setDraft({ ...draft, params: { ...draft.params, [k]: v } });
  const changed = !!(active && draft && JSON.stringify(active.params) !== JSON.stringify(draft.params));
  const strat = strategies.find((x) => x.id === draft?.params.strategy);
  const fields = draft ? [...(strat?.params ?? []), ...SHARED]
    .filter((k) => !(k === "stop_buffer_pct" && draft.params.strategy === "trend_following")).map((k) => F[k]).filter(Boolean) : [];
  function pick(st: StrategyInfo) { setText(st.template); translate(st.template); }

  return (
    <div className="strategy">
      {notify && <StrategyResearch notify={notify} onApplied={() => { load(); onSaved(); }} />}
      <label className="lbl">Start from a strategy</label>
      <div className="stratpick" role="group" aria-label="Strategy">
        {strategies.map((st) => (
          <button key={st.id} className={draft?.params.strategy === st.id ? "on" : ""} onClick={() => pick(st)} title={st.style}>
            <b>{st.name}</b><span>{st.style}</span>
          </button>
        ))}
      </div>
      <label className="lbl" htmlFor="nl">Or describe your setup in everyday language</label>
      <textarea id="nl" rows={4} value={text} onChange={(e) => setText(e.target.value)} />
      <button className="btn" onClick={() => translate()}>Translate to rules</button>

      {draft && (
        <>
          <h3>What your words mean here</h3>
          <ul className="interp">
            {draft.interpretations.map((i) => (
              <li key={i.phrase}><q>{i.phrase}</q><span>{i.definition}</span></li>
            ))}
          </ul>
          <h3>Editable rule parameters <span className="muted small">{strat?.name}</span></h3>
          {strat && <p className="small muted">Stop: {strat.stop_rule}</p>}
          <div className="params">
            {fields.map((f) => (
              <label key={f.key} title={f.help}>
                <span>{f.label}</span>
                <span className="inp">
                  <input type="number" step={f.step}
                    value={f.scale ? Math.round((draft.params[f.key] as number) * f.scale * 100) / 100 : (draft.params[f.key] as number)}
                    onChange={(e) => set(f.key, f.scale ? Number(e.target.value) / f.scale : Number(e.target.value))} />
                  <em>{f.suffix}</em>
                </span>
              </label>
            ))}
            <fieldset>
              <legend>When earnings fall before the planned exit</legend>
              <label><input type="radio" checked={draft.params.earnings_mode === "warn"} onChange={() => set("earnings_mode", "warn")} /> Show a warning, keep the stock</label>
              <label><input type="radio" checked={draft.params.earnings_mode === "exclude"} onChange={() => set("earnings_mode", "exclude")} /> Exclude the stock</label>
              <p className="small muted">A missing earnings date is always labelled unknown and never treated as clear.</p>
            </fieldset>
          </div>
          {err && <p className="err">{err}</p>}
          <button className="btn primary" onClick={save}>
            Confirm and save as v{(history[0]?.version ?? 0) + 1}
          </button>
          {active && <p className="small muted">Active: v{active.version}, confirmed {active.confirmed_at?.replace("T", " ").slice(0, 16)} UTC{changed ? " · unsaved edits" : ""}</p>}
          {history.length > 0 && (
            <details className="history">
              <summary>Version history ({history.length})</summary>
              <table className="mini">
                <thead><tr><th>v</th><th>Strategy</th><th>Hold</th><th>Earnings</th></tr></thead>
                <tbody>{history.map((h) => (
                  <tr key={h.version}><td>{h.version}</td><td>{h.name}</td>
                    <td>{h.params.hold_sessions}</td><td>{h.params.earnings_mode}</td></tr>))}</tbody>
              </table>
            </details>
          )}
        </>
      )}
    </div>
  );
}
