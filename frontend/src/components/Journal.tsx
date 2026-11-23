import { useEffect, useState } from "react";
import { api } from "../api";
import type { Level } from "../App";
import { OrderStatus, ResultBadge, money, pct } from "./ui";

interface Summ { trades: number; open: number; closed: number; realized_pnl: number; win_rate_pct: number | null; avg_win: number | null;
  avg_loss: number | null; profit_factor: number | null; avg_return_pct: number | null; plan_followed_pct: number | null }
interface Pnl { all: Summ; stocks_swing: Summ; stocks_day: Summ; crypto: Summ; by_venue: Record<string, Summ>; note: string; excluded_non_usd_crypto: number }

function PnlStats({ tick }: { tick: number }) {
  const [p, setP] = useState<Pnl | null>(null);
  useEffect(() => { api.get<Pnl>("/api/pnl").then(setP).catch(() => {}); }, [tick]);
  if (!p || !p.all.trades) return null;
  const rows: [string, Summ][] = [["All", p.all], ["Swing (stocks)", p.stocks_swing], ["Day (stocks)", p.stocks_day], ["Crypto", p.crypto],
    ...Object.entries(p.by_venue).map(([k, v]) => [`· ${k}`, v] as [string, Summ])];
  const v = (x: number | null, f: (n: number) => string) => (x == null ? "—" : f(x));
  return (
    <section className="card pnl">
      <h3>P&amp;L</h3>
      <table className="mini"><thead><tr><th></th><th>Closed / open</th><th>Realized</th><th>Win rate</th><th>Avg win / loss</th><th>Profit factor</th><th>Plan followed</th></tr></thead>
        <tbody>{rows.filter(([, s]) => s.trades).map(([k, s]) => (
          <tr key={k}><td>{k}</td><td className="num">{s.closed} / {s.open}</td>
            <td className={`num ${s.realized_pnl >= 0 ? "gain" : "loss"}`}>{money(s.realized_pnl)}</td>
            <td className="num">{v(s.win_rate_pct, (n) => `${n}%`)}</td>
            <td className="num">{v(s.avg_win, money)} / {v(s.avg_loss, money)}</td>
            <td className="num">{v(s.profit_factor, (n) => n.toFixed(2))}</td>
            <td className="num">{v(s.plan_followed_pct, (n) => `${n}%`)}</td></tr>))}</tbody></table>
      <p className="small muted">{p.note}{p.excluded_non_usd_crypto ? ` ${p.excluded_non_usd_crypto} crypto trade(s) in non-dollar quotes aren't in the totals.` : ""}</p>
    </section>
  );
}

function CryptoEntry({ j }: { j: any }) {
  const closed = j.status === "closed";
  return (
    <article className="entry card">
      <header className="row between">
        <h3>{j.symbol} <span className="kind-tag">crypto</span> · {j.created_at.replace("T", " ").slice(0, 16)} UTC</h3>
        <span className={`jstatus js-${j.status}`}>{closed ? "Closed" : "Open"}</span>
      </header>
      <div className="cols">
        <section>
          <h4>At entry</h4>
          {j.thesis ? <blockquote>{j.thesis}</blockquote> : <p className="small muted">No thesis written.</p>}
          <p className="small">{j.venue} · {j.environment} · stop {j.plan.stop_price ?? "none"}</p>
        </section>
        <section>
          <h4>What happened</h4>
          <dl className="figs">
            <dt>Entry</dt><dd className="num">{j.amount} @ {j.entry_price} {j.quote}</dd>
            <dt>Exit</dt><dd className="num">{closed ? `${j.exit_price} ${j.quote} (${j.exit_reason.replace("_", " ")})` : "position open"}</dd>
            {closed && <><dt>Realized P&L</dt><dd className={`num ${j.realized_pnl >= 0 ? "gain" : "loss"}`}>{j.realized_pnl} {j.quote} ({pct(j.return_pct, 2)}) <small>fees {j.fees}</small></dd></>}
          </dl>
          <div className={`adherence ${j.adherence.followed ? "ok" : "dev"}`}>
            <ul>{j.adherence.checks.map((c: any) => <li key={c.check} className={c.ok ? "ok" : "no"}>{c.ok ? "✓" : "✕"} {c.check} <span className="muted">— {c.detail}</span></li>)}</ul>
          </div>
        </section>
      </div>
    </article>
  );
}

export function Journal({ entries, level, onChanged, tick = 0 }: { entries: any[]; level: Level; onChanged: () => void; tick?: number }) {
  const [crypto, setCrypto] = useState<any[]>([]);
  useEffect(() => { api.get<any[]>("/api/crypto/journal").then(setCrypto).catch(() => {}); }, [tick]);
  const items = [...entries.map((j) => ({ t: j.created_at, el: <Entry key={`s${j.journal_id}`} j={j} level={level} onChanged={onChanged} /> })),
    ...crypto.map((j) => ({ t: j.created_at, el: <CryptoEntry key={`c${j.id}`} j={j} /> }))].sort((a, b) => (a.t < b.t ? 1 : -1));
  if (!items.length) return <p className="muted pad">The journal fills in when you submit an order from a qualifying setup or a crypto order.</p>;
  return <div className="journal"><PnlStats tick={tick} />{items.map((x) => x.el)}</div>;
}

function Entry({ j, level, onChanged }: { j: any; level: Level; onChanged: () => void }) {
  const [note, setNote] = useState(j.review_note ?? "");
  const ev = j.evidence;
  const f = ev.facts;
  const closed = j.status === "closed";
  return (
    <article className="entry card">
      <header className="row between">
        <h3>{j.symbol} · {j.created_at.replace("T", " ").slice(0, 16)} UTC</h3>
        <span className={`jstatus js-${j.status}`}>{{ open: "Open", closed: "Closed", not_filled: "Not filled" }[j.status as string]}</span>
      </header>

      <div className="cols">
        <section>
          <h4>What you knew at entry <span className="lock" title="Stored write-once; the database rejects edits">🔒 recorded</span></h4>
          <blockquote>{j.thesis}</blockquote>
          <p className="small">Rules v{j.ruleset_version} · status <b>{ev.status.replace(/_/g, " ").toLowerCase()}</b> · {j.data_timestamp.split(" | ")[0]}</p>
          <table className="mini">
            <tbody>
              {ev.rules.map((r: any) => (
                <tr key={r.id}><td>{r.name}</td><td className="num">{r.observed}</td><td><ResultBadge r={r.result} /></td></tr>
              ))}
            </tbody>
          </table>
          {level === "analyst" && (
            <p className="small muted num">{ev.strategy_name ?? "EMA pullback"} · {Object.entries(f).filter(([k]) => !["bar", "plan", "earnings", "avg_volume"].includes(k))
              .map(([, x]: any) => `${x.label}: ${typeof x.value === "number" ? x.value.toFixed(2) : x.value ?? "n/a"}`).join(" · ")} · bar L {f.bar.value.low} C {f.bar.value.close}</p>
          )}
          {j.plan.risk_overrides?.length > 0 && (
            <p className="small warnc">Risk overrides at entry: {j.plan.risk_overrides.map((o: any) => `${o.check.replace(/_/g, " ")} (${o.observed}): “${o.reason}”`).join("; ")}</p>
          )}
          <p className="small">Plan: buy {j.plan.qty} at next open (assumed {money(j.plan.entry_assumption)}), stop {money(j.plan.stop)}, exit {j.plan.exit_date} close. Est. loss if stopped {money(j.plan.est_loss_if_stopped)}.</p>
        </section>

        <section>
          <h4>What happened</h4>
          <p className="small">Order <code>{j.order_id}</code> <OrderStatus s={j.order_detail.status} /></p>
          {j.entry_date ? (
            <dl className="figs">
              <dt>Entry</dt><dd className="num">{j.entry_date} · {j.qty} @ {money(j.entry_price)}</dd>
              <dt>Exit</dt><dd className="num">{closed ? `${j.exit_date} · ${money(j.exit_price)} (${j.exit_reason.replace("_", " ")})` : "position open"}</dd>
              {closed && <><dt>Realized P&L</dt><dd className={`num ${j.realized_pnl >= 0 ? "gain" : "loss"}`}>{money(j.realized_pnl)} ({pct(j.return_pct, 2)})</dd></>}
            </dl>
          ) : <p className="small muted">{j.status === "not_filled" ? "Entry never filled." : "Waiting for the next simulated open."}</p>}
          {j.adherence && (
            <div className={`adherence ${j.adherence.followed ? "ok" : "dev"}`}>
              <b>{j.adherence.followed ? "Followed the plan" : "Deviated from the plan"} ({j.adherence.score})</b>
              <ul>{j.adherence.checks.map((c: any) => <li key={c.check} className={c.ok ? "ok" : "no"}>{c.ok ? "✓" : "✕"} {c.check} <span className="muted">— {c.detail}</span></li>)}</ul>
            </div>
          )}
          {closed && (
            <div className="review">
              <label className="lbl">Review note (editable; the entry reasoning is not)</label>
              <textarea rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
              <button className="btn small" onClick={async () => { await api.post(`/api/journal/${j.journal_id}/note`, { note }); onChanged(); }}>Save note</button>
            </div>
          )}
        </section>
      </div>
    </article>
  );
}
