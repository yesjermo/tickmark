import { useCallback, useEffect, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Check, ChevronRight } from "lucide-react";
import { api, ApiError } from "../api";
import { ConnectPortal, Item, Monogram, Provider } from "./ConnectPortal";
import { TradingViewConnect } from "./TradingViewConnect";

const STATE: Record<string, string> = { connected: "Connected", available: "Connect", setup: "Needs server setup", planned: "Planned",
  unavailable: "No direct API" };

export function Connections({ notify, goTab, openTicket, symbol }: { notify: (s: string) => void; goTab: (side: string) => void; openTicket: (sym: string) => void; symbol?: string }) {
  const [d, setD] = useState<{ groups: { id: string; name: string }[]; items: Item[] } | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [tv, setTv] = useState<{ connected: boolean; signals: unknown[] } | null>(null);
  useEffect(() => { api.get<any>("/api/tradingview").then(setTv).catch(() => setTv(null)); }, [open]);
  const load = useCallback(() => api.get<any>("/api/connections").then(setD).catch((e) => notify((e as ApiError).message)), [notify]);
  useEffect(() => { load(); api.get<any>("/api/linked").then((r) => setProviders(r.providers)).catch(() => {}); }, [load]);
  const close = useCallback(() => setOpen(null), []);

  if (!d) return <div className="pad muted">Loading connections…</div>;
  const item = d.items.find((i) => i.id === open) ?? null;
  return (
    <div className="connections">
      <header className="conn-head">
        <h2>Connections</h2>
        <p className="muted small">Pick a broker, exchange or AI to connect. Each opens its own sign-in. Once an account is connected you can switch
          real-money trading on for it in the same window.</p>
      </header>
      <section className="conn-group">
        <h3>Charts and signals</h3>
        <ul className="conn-tiles">
          <li><button className={`conn-tile st-${tv?.connected ? "connected" : "available"}`} onClick={() => setOpen("tradingview")}>
            <Monogram name="Trading View" />
            <span className="grow"><b>TradingView</b><small>Sign in, open your own charts, receive your alerts</small></span>
            <span className={`conn-pill st-${tv?.connected ? "connected" : "available"}`}>{tv?.connected && <Check size={12} aria-hidden />}{tv?.connected ? "Connected" : "Connect"}</span>
            <ChevronRight size={15} className="chev" aria-hidden />
          </button></li>
        </ul>
      </section>
      {d.groups.map((g) => {
        const items = d.items.filter((i) => i.group === g.id);
        if (!items.length) return null;
        return (
          <section key={g.id} className="conn-group">
            <h3>{g.name}</h3>
            <ul className="conn-tiles">
              {items.map((it, n) => (
                <motion.li key={it.id} initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }}
                  transition={{ duration: 0.18, delay: Math.min(n, 8) * 0.025, ease: [0.2, 0.8, 0.2, 1] }}>
                  <button className={`conn-tile st-${it.state}`} onClick={() => setOpen(it.id)} disabled={it.state === "unavailable" && !it.methods.length}>
                    <Monogram name={it.name} />
                    <span className="grow"><b>{it.name}</b><small>{it.segment}</small></span>
                    <span className={`conn-pill st-${it.state}`}>{it.state === "connected" && <Check size={12} aria-hidden />}{STATE[it.state]}</span>
                    <ChevronRight size={15} className="chev" aria-hidden />
                  </button>
                </motion.li>
              ))}
            </ul>
          </section>
        );
      })}
      <AnimatePresence>
        {open === "tradingview" && <TradingViewConnect key="tv" notify={notify} onClose={close} openTicket={openTicket} symbol={symbol} />}
        {item && <ConnectPortal key={item.id} item={item} providers={providers} notify={notify} onClose={close} onChanged={load} goTab={goTab} />}
      </AnimatePresence>
    </div>
  );
}
