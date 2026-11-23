import { useEffect, useState } from "react";
import { Bot, Check, ChevronDown, ExternalLink, KeyRound, LogIn, Server, Wallet } from "lucide-react";
import { api, ApiError } from "../api";
import { money } from "./ui";

interface Method { kind: string; label: string; available: boolean; connected: boolean; provider?: string; target?: string; why?: string | null;
  detail?: string; used?: number; limit?: number; period?: string; accounts?: { id: string; label: string; sandbox: boolean; live: boolean; total_usd: number | null }[] }
interface Item { id: string; name: string; group: string; segment: string; assets: string; how: string; state: string; methods: Method[]; trading?: boolean }
interface Field { key: string; label: string; secret: boolean; default: string | null; placeholder: string | null }
interface Provider { id: string; name: string; sandbox: boolean; fields: Field[]; help: string; kind: string }

const STATE: Record<string, string> = { connected: "Connected", available: "Not connected", setup: "Needs server setup", planned: "Planned",
  unavailable: "No direct API" };
const KIND_ICON: Record<string, typeof KeyRound> = { oauth: LogIn, api_key: KeyRound, gateway: Server, address: Wallet, plan: Bot, local: Server, connector: ExternalLink, token: KeyRound };

function KeyForm({ target, providers, onDone, notify }: { target: string; providers: Provider[]; onDone: () => void; notify: (s: string) => void }) {
  const p = providers.find((x) => x.id === target);
  const alpaca = target === "alpaca_paper";
  const fields: Field[] = alpaca ? [{ key: "key_id", label: "API key ID", secret: false, default: null, placeholder: "PK…" },
    { key: "secret", label: "Secret key", secret: true, default: null, placeholder: null }] : p?.fields ?? [];
  const [vals, setVals] = useState<Record<string, string>>({});
  const [sandbox, setSandbox] = useState(true);
  const [busy, setBusy] = useState(false);
  async function go() {
    setBusy(true);
    try {
      if (alpaca) await api.post("/api/broker/connect", { key_id: vals.key_id, secret: vals.secret });
      else await api.post("/api/linked", { provider: target, sandbox, values: vals });
      notify(alpaca ? "Alpaca paper connected." : `${p?.name} linked. SwingDesk reads it; trading real money needs its switch in Broker → Linked accounts.`);
      onDone();
    } catch (e) { notify((e as ApiError).message); } finally { setBusy(false); }
  }
  return (
    <form className="conn-form" onSubmit={(e) => { e.preventDefault(); go(); }}>
      {p?.help && <p className="small muted">{p.help}</p>}
      {alpaca && <p className="small muted">Alpaca dashboard → Paper trading → API keys. Keys are checked with a read-only call, then stored encrypted.</p>}
      <div className="conn-fields">
        {fields.map((f) => (
          <label key={f.key}>{f.label}
            <input type={f.secret ? "password" : "text"} autoComplete="off" placeholder={f.placeholder ?? ""} value={vals[f.key] ?? f.default ?? ""}
              onChange={(e) => setVals({ ...vals, [f.key]: e.target.value })} />
          </label>
        ))}
      </div>
      <div className="row">
        {p?.sandbox && <label className="inline"><input type="checkbox" checked={sandbox} onChange={(e) => setSandbox(e.target.checked)} /> Testnet / demo account</label>}
        <button className="btn primary small" disabled={busy}>{busy ? "Checking…" : "Check and connect"}</button>
      </div>
    </form>
  );
}

export function Connections({ notify, goTab }: { notify: (s: string) => void; goTab: (side: string) => void }) {
  const [d, setD] = useState<{ groups: { id: string; name: string }[]; items: Item[] } | null>(null);
  const [providers, setProviders] = useState<Provider[]>([]);
  const [open, setOpen] = useState<string | null>(null);
  const [form, setForm] = useState<string | null>(null);
  const load = () => api.get<any>("/api/connections").then(setD).catch((e) => notify((e as ApiError).message));
  useEffect(() => { load(); api.get<any>("/api/linked").then((r) => setProviders(r.providers)).catch(() => {}); }, []);

  async function act(it: Item, m: Method) {
    if (m.kind === "oauth") {
      if (!m.available) return notify(m.why ?? "Not offered on this server yet.");
      try { const r = await api.get<{ authorize_url: string }>(`/api/oauth/${m.provider}/start`); location.href = r.authorize_url; }
      catch (e) { notify((e as ApiError).message); }
      return;
    }
    if (m.kind === "api_key" && m.target && m.target !== "alpaca_live" && m.target !== "anthropic") { setOpen(it.id); setForm(m.target); return; }
    if (m.target === "alpaca_live" || m.target === "ibkr") return goTab("broker");
    if (m.kind === "plan" || m.kind === "local" || m.target === "anthropic") return goTab("copilot");
    if (m.kind === "connector" || m.kind === "token") return goTab("account");
    if (m.kind === "gateway" || m.kind === "address") { setOpen(it.id); setForm(m.target ?? null); }
  }

  if (!d) return <div className="pad muted">Loading connections…</div>;
  return (
    <div className="connections">
      <header className="conn-head">
        <h2>Connections</h2>
        <p className="muted small">Sign in to your brokers, exchanges and AI. Signing in or pasting a key only lets SwingDesk read the account;
          real-money trading stays off until you turn on that account's switch.</p>
      </header>
      {d.groups.map((g) => {
        const items = d.items.filter((i) => i.group === g.id);
        if (!items.length) return null;
        return (
          <section key={g.id} className="conn-group">
            <h3>{g.name}</h3>
            <ul className="conn-list">
              {items.map((it) => {
                const isOpen = open === it.id;
                const best = it.methods.find((m) => m.available && !m.connected) ?? it.methods.find((m) => m.available);
                return (
                  <li key={it.id} className={`conn-row st-${it.state} ${isOpen ? "open" : ""}`}>
                    <div className="conn-main">
                      <button className="conn-name" onClick={() => { setOpen(isOpen ? null : it.id); setForm(null); }} aria-expanded={isOpen}>
                        <ChevronDown size={14} className="chev" aria-hidden />
                        <span><b>{it.name}</b><span className="small muted"> · {it.segment}</span></span>
                      </button>
                      <span className={`conn-state st-${it.state}`}>{it.state === "connected" && <Check size={12} aria-hidden />}{STATE[it.state]}</span>
                      <div className="conn-actions">
                        {it.methods.filter((m) => m.kind === "oauth" || m === best).slice(0, 2).map((m, i) => {
                          const Icon = KIND_ICON[m.kind] ?? KeyRound;
                          return (
                            <button key={i} className={`btn small ${m.kind === "oauth" && m.available ? "primary" : i === 0 && m.available ? "" : "ghost"}`}
                              disabled={!m.available && m.kind !== "oauth"} title={m.why ?? ""} onClick={() => act(it, m)}>
                              <Icon size={13} aria-hidden /> {m.label}{m.connected && m.kind !== "plan" && <Check size={12} aria-label="connected" />}
                            </button>
                          );
                        })}
                      </div>
                    </div>
                    {isOpen && (
                      <div className="conn-detail">
                        <p className="small">{it.how}</p>
                        <p className="small muted">{it.assets}</p>
                        <ul className="conn-methods">
                          {it.methods.map((m, i) => {
                            const Icon = KIND_ICON[m.kind] ?? KeyRound;
                            return (
                              <li key={i}>
                                <Icon size={14} aria-hidden />
                                <span className="grow">{m.label}{m.detail && <span className="muted small"> · {m.detail}</span>}
                                  {m.limit != null && m.available && <span className="muted small"> · {m.used}/{m.limit} this {m.period}</span>}
                                  {!m.available && <span className="muted small"> · {m.why ?? "not offered here"}</span>}
                                  {m.accounts?.map((a) => <span key={a.id} className="acct-chip">{a.label}{a.live ? " · real money" : a.sandbox ? " · testnet" : " · read-only"}{a.total_usd != null ? ` · ${money(a.total_usd)}` : ""}</span>)}
                                </span>
                                <button className="btn small ghost" disabled={!m.available && m.kind !== "oauth"} onClick={() => act(it, m)}>{m.connected ? "Manage" : "Connect"}</button>
                              </li>
                            );
                          })}
                          {it.methods.length === 0 && <li className="muted small">SwingDesk can't connect to {it.name}: it has no public API for this.</li>}
                        </ul>
                        {form && <KeyForm key={form} target={form} providers={providers} notify={notify} onDone={() => { setForm(null); load(); }} />}
                      </div>
                    )}
                  </li>
                );
              })}
            </ul>
          </section>
        );
      })}
    </div>
  );
}
