import type { Status_ } from "../api";
import { workspaceOf } from "./TopBar";

const usd = (n: number) => n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });

/** The desk's one dark block: what the account is worth now and how it got there. */
export function AccountCard({ status }: { status: Status_ | null }) {
  const ws = workspaceOf(status);
  const a = status?.account;
  const change = a ? a.equity - a.starting_equity : 0;
  const pct = a && a.starting_equity ? (change / a.starting_equity) * 100 : 0;
  const label = ws === "trading" ? "Live account" : ws === "test" ? "Paper account" : "Backtest account";
  return (
    <section className="acct-card" aria-label={label}>
      <div className="acct-k">{label}</div>
      <div className="acct-v num">{a ? usd(a.equity) : "—"}<span>USD</span></div>
      <div className={`acct-d num ${change > 0 ? "up" : change < 0 ? "down" : ""}`}>
        {a ? `${change >= 0 ? "+" : "−"}${usd(Math.abs(change))} (${pct >= 0 ? "+" : "−"}${Math.abs(pct).toFixed(2)}%)` : ""}
        <span> since start</span>
      </div>
      <dl className="acct-grid">
        <div><dt>Cash</dt><dd className="num">{a ? usd(a.cash) : "—"}</dd></div>
        <div><dt>Buying power</dt><dd className="num">{a ? usd(a.buying_power) : "—"}</dd></div>
        <div><dt>Open positions</dt><dd className="num">{a ? a.positions.length : "—"}</dd></div>
      </dl>
    </section>
  );
}
