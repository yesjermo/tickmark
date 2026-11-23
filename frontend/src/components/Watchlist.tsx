import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { Screen } from "../App";
import { StatusChip, num } from "./ui";

export function Watchlist({ screen, live, selected, onSelect, onChanged, notify }: {
  screen: Screen | null; live: boolean; selected: string; onSelect: (s: string) => void; onChanged: () => void; notify: (s: string) => void;
}) {
  const [add, setAdd] = useState("");
  const [busy, setBusy] = useState(false);
  const [quotes, setQuotes] = useState<Record<string, { price: number; t: string | null; feed?: string; change_pct: number | null }>>({});
  useEffect(() => {
    if (!live) { setQuotes({}); return; }
    const load = () => api.get<{ quotes: typeof quotes }>("/api/quotes").then((r) => setQuotes(r.quotes)).catch(() => {});
    load();
    const id = setInterval(load, 60000);              // latest trades refresh every minute
    return () => clearInterval(id);
  }, [live]);
  const et = (iso: string) => new Date(iso).toLocaleString("en-US", { weekday: "short", hour: "numeric", minute: "2-digit", timeZone: "America/New_York" }) + " ET";
  if (!screen) return <div className="pad muted">Loading…</div>;
  const symbols = [...screen.results.map((r) => r.symbol), ...Object.keys(screen.errors ?? {})];

  async function save(next: string[]) {
    setBusy(true);
    try {
      const r = await api.put<{ watchlist: string[]; errors: Record<string, string> }>("/api/watchlist", { symbols: next });
      const bad = Object.entries(r.errors);
      notify(bad.length ? `Couldn't load ${bad.map(([k, v]) => `${k} (${v})`).join(", ")}` : `Watchlist: ${r.watchlist.join(", ")}`);
      setAdd("");
      onChanged();
    } catch (e) { notify((e as ApiError).message); } finally { setBusy(false); }
  }

  return (
    <div>
      <div className="section-head">
        <h2>Watchlist</h2>
        <span className="muted small">rules {screen.ruleset.version ? `v${screen.ruleset.version}` : "draft"}</span>
      </div>
      {live && (
        <form className="addsym" onSubmit={(e) => { e.preventDefault(); if (add.trim()) save([...symbols, add.trim()]); }}>
          <input value={add} onChange={(e) => setAdd(e.target.value.toUpperCase())} placeholder="Add ticker, e.g. AAPL" aria-label="Add ticker" maxLength={10} />
          <button className="btn small" disabled={busy || !add.trim()}>{busy ? "…" : "Add"}</button>
        </form>
      )}
      <ul className="watch">
        {screen.results.map((r) => {
          const bar = r.facts.bar.value;
          return (
            <li key={r.symbol}>
              <button className={selected === r.symbol ? "on" : ""} onClick={() => onSelect(r.symbol)}>
                <div className="row1">
                  <b>{r.symbol}</b>
                  <span className="num">{num(bar.close)}</span>
                </div>
                <div className="row2">
                  <StatusChip s={r.status} />
                  {quotes[r.symbol] && (
                    <span className={`quote num ${(quotes[r.symbol].change_pct ?? 0) >= 0 ? "gain" : "loss"}`}
                      title={quotes[r.symbol].feed === "yahoo" ? "Latest Yahoo Finance price (may be delayed up to 15 minutes). Rules still use the completed daily close." : `Latest trade (IEX feed) at ${et(quotes[r.symbol].t!)}. Rules still use the completed daily close.`}>
                      {num(quotes[r.symbol].price)}{quotes[r.symbol].change_pct != null && ` ${quotes[r.symbol].change_pct! >= 0 ? "+" : ""}${quotes[r.symbol].change_pct}%`}
                    </span>
                  )}
                  <span className="dots" aria-label="rule results">
                    {r.rules.map((x) => <i key={x.id} className={`d-${x.result}`} title={`${x.name}: ${x.result}`} />)}
                  </span>
                </div>
                {screen.quality?.[r.symbol] && !screen.quality[r.symbol].ok && (
                  <div className="dq" title={screen.quality[r.symbol].issues.map((i) => `${i.date ?? ""} ${i.kind.replace(/_/g, " ")}: ${i.detail}`).join("\n")}>
                    ⚠ data: {screen.quality[r.symbol].issues.slice(-1).map((i) => i.kind.replace(/_/g, " "))[0]}</div>
                )}
                {r.status === "NEAR_MISS" && (
                  <div className="why">Dropped: {r.rules.find((x) => r.failed.includes(x.id))?.name.toLowerCase()}</div>
                )}
                {r.status === "NEEDS_REVIEW" && (
                  <div className="why">Unknown: {r.rules.filter((x) => x.result === "unknown")
                    .map((x) => (x.id === "earnings" ? "next earnings date" : x.id === "data" && x.margin ? x.margin : x.name.toLowerCase())).join(", ")}</div>
                )}
              </button>
              {live && symbols.length > 1 && (
                <button className="rm" title={`Remove ${r.symbol}`} aria-label={`Remove ${r.symbol}`} disabled={busy}
                  onClick={() => save(symbols.filter((s) => s !== r.symbol))}>×</button>
              )}
            </li>
          );
        })}
        {Object.entries(screen.errors ?? {}).map(([sym, err]) => (
          <li key={sym} className="errsym"><b>{sym}</b> <span className="small">{err}</span>
            {live && <button className="rm" onClick={() => save(symbols.filter((s) => s !== sym))} aria-label={`Remove ${sym}`}>×</button>}</li>
        ))}
      </ul>
      <p className="legend small muted">
        {screen.as_of ? `Screened on the completed daily bar of ${screen.as_of}.` : "No data loaded yet."} Dots: one per rule, in order.
        {live && " Rules use completed bars only; the colored figure is the latest price (Alpaca IEX with a key, otherwise Yahoo), refreshed each minute."}
      </p>
    </div>
  );
}
