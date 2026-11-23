import { useEffect, useState } from "react";
import { api, ApiError } from "../api";

interface RiskState {
  limits: { version: number; params: Record<string, any>; note: string; created_at: string | null };
  history: { version: number; params: Record<string, any>; note: string; created_at: string }[];
  kill_switch: { on: boolean; reason?: string | null; at?: string };
  overrides: { id: number; client_order_id: string; symbol: string; check_id: string; observed: string; threshold: string; reason: string; created_at: string }[];
}

const FIELDS: { key: string; label: string; suffix: string; step: number; kind: string }[] = [
  { key: "max_open_positions", label: "Max open positions", suffix: "positions", step: 1, kind: "soft" },
  { key: "max_portfolio_heat_pct", label: "Max total open risk", suffix: "% of equity", step: 0.1, kind: "soft" },
  { key: "max_sector_pct", label: "Max in one sector", suffix: "% of equity", step: 1, kind: "soft" },
  { key: "daily_loss_limit_pct", label: "Daily loss limit", suffix: "% of equity", step: 0.1, kind: "hard" },
  { key: "pdt_equity_min", label: "Pattern-day-trader minimum", suffix: "$ equity", step: 1000, kind: "soft" },
];

export function RiskPanel({ onChanged, notify }: { onChanged: () => void; notify: (s: string) => void }) {
  const [r, setR] = useState<RiskState | null>(null);
  const [edit, setEdit] = useState<Record<string, any>>({});
  const [restricted, setRestricted] = useState("");
  const [note, setNote] = useState("");
  const [ksReason, setKsReason] = useState("");
  const load = () => api.get<RiskState>("/api/risk").then((x) => {
    setR(x); setEdit(x.limits.params); setRestricted((x.limits.params.restricted ?? []).join(", "));
  });
  useEffect(() => { load(); }, []);
  if (!r) return <p className="muted pad">Loading…</p>;

  async function save() {
    try {
      const params = { ...edit, restricted: restricted.split(/[\s,]+/).filter(Boolean) };
      const v = await api.put<any>("/api/risk/limits", { params, note });
      notify(`Risk limits saved as v${v.version}.`); setNote(""); load(); onChanged();
    } catch (e) { notify((e as ApiError).message); }
  }
  async function kill(on: boolean) {
    try {
      await api.put("/api/risk/kill-switch", { on, reason: ksReason });
      notify(on ? "Kill switch ON: no new entries until you turn it off." : "Kill switch off.");
      setKsReason(""); load(); onChanged();
    } catch (e) { notify((e as ApiError).message); }
  }

  return (
    <div className="riskp">
      <div className={`card ks ${r.kill_switch.on ? "on" : ""}`}>
        <div className="row between">
          <b>{r.kill_switch.on ? "⛔ Kill switch ON" : "Kill switch off"}</b>
          {r.kill_switch.on
            ? <button className="btn small" onClick={() => kill(false)}>Turn off</button>
            : <button className="btn small danger" disabled={ksReason.trim().length < 3} onClick={() => kill(true)}>Stop all new entries</button>}
        </div>
        {r.kill_switch.on
          ? <p className="small">Since {r.kill_switch.at?.replace("T", " ").slice(0, 16)} UTC: {r.kill_switch.reason}</p>
          : <input className="ksreason" value={ksReason} onChange={(e) => setKsReason(e.target.value)} placeholder="Reason (required, kept on record)" />}
      </div>

      <h3>Limits <span className="muted small">v{r.limits.version}{r.limits.version === 0 ? " (defaults)" : ""}</span></h3>
      <p className="small muted"><b>Hard</b> blocks can't be overridden. <b>Soft</b> blocks need a written reason, which is logged and copied into the journal.</p>
      <div className="params">
        {FIELDS.map((f) => (
          <label key={f.key}>
            <span>{f.label} <em className={`kind ${f.kind}`}>{f.kind}</em></span>
            <span className="inp"><input type="number" step={f.step} value={edit[f.key] ?? ""}
              onChange={(e) => setEdit({ ...edit, [f.key]: Number(e.target.value) })} /><em>{f.suffix}</em></span>
          </label>
        ))}
      </div>
      <label className="lbl" htmlFor="restr">Restricted symbols <em className="kind hard">hard</em></label>
      <textarea id="restr" rows={2} value={restricted} onChange={(e) => setRestricted(e.target.value.toUpperCase())} placeholder="e.g. ACME, XYZ" />
      <p className="small muted">Also always hard: broker account blocked, not enough buying power.</p>
      <input value={note} onChange={(e) => setNote(e.target.value)} placeholder="Why are you changing limits? (optional)" />
      <button className="btn primary" style={{ marginTop: 8 }} onClick={save}>Save as v{r.limits.version + 1}</button>

      {r.history.length > 0 && (
        <details className="history"><summary>Limit history ({r.history.length})</summary>
          <table className="mini"><thead><tr><th>v</th><th>Pos</th><th>Heat</th><th>Sector</th><th>Loss</th><th>Note</th></tr></thead>
            <tbody>{r.history.map((h) => (
              <tr key={h.version}><td>{h.version}</td><td>{h.params.max_open_positions}</td><td>{h.params.max_portfolio_heat_pct}%</td>
                <td>{h.params.max_sector_pct}%</td><td>{h.params.daily_loss_limit_pct}%</td><td>{h.note}</td></tr>))}</tbody></table>
        </details>
      )}

      <h3>Override log <span className="muted small">append-only</span></h3>
      {r.overrides.length === 0 ? <p className="small muted">No overrides yet.</p> : (
        <ul className="ovlog">{r.overrides.map((o) => (
          <li key={o.id}><span className="num small muted">{o.created_at.replace("T", " ").slice(0, 16)}</span> <b>{o.symbol !== "-" ? o.symbol : ""} {o.check_id.replace(/_/g, " ")}</b>
            {o.observed && o.observed !== "-" && <span className="small"> {o.observed} (limit {o.threshold})</span>}
            <div className="small">“{o.reason}”</div></li>))}</ul>
      )}
    </div>
  );
}
