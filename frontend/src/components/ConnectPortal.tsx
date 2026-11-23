import { useEffect, useRef, useState } from "react";
import { useDialog } from "./theme";
import { motion } from "motion/react";
import { Bot, Check, ExternalLink, KeyRound, LogIn, Server, ShieldAlert, Wallet, X } from "lucide-react";
import { api, ApiError } from "../api";
import { money } from "./ui";

export interface Method { kind: string; label: string; available: boolean; connected: boolean; provider?: string; target?: string; why?: string | null;
  detail?: string; used?: number; limit?: number; period?: string; accounts?: { id: string; label: string; sandbox: boolean; live: boolean; total_usd: number | null }[] }
export interface Item { id: string; name: string; group: string; segment: string; assets: string; how: string; state: string; methods: Method[]; trading?: boolean }
interface Field { key: string; label: string; secret: boolean; default: string | null; placeholder: string | null }
export interface Provider { id: string; name: string; sandbox: boolean; fields: Field[]; help: string; kind: string }

const KIND_ICON: Record<string, typeof KeyRound> = { oauth: LogIn, api_key: KeyRound, gateway: Server, address: Wallet, plan: Bot, local: Server, connector: ExternalLink, token: KeyRound };

export function Monogram({ name }: { name: string }) {
  const letters = name.replace(/\(.*?\)/g, "").split(/[\s/.]+/).filter(Boolean).slice(0, 2).map((w) => w[0]).join("").toUpperCase();
  return <span className="mono-badge" aria-hidden>{letters}</span>;
}

function KeyForm({ target, providers, onDone, notify }: { target: string; providers: Provider[]; onDone: (r: any) => void; notify: (s: string) => void }) {
  const p = providers.find((x) => x.id === target);
  const alpaca = target === "alpaca_paper" || target === "alpaca_live";
  const fields: Field[] = alpaca ? [{ key: "key_id", label: "API key ID", secret: false, default: null, placeholder: target === "alpaca_live" ? "AK…" : "PK…" },
    { key: "secret", label: "Secret key", secret: true, default: null, placeholder: null }] : p?.fields ?? [];
  const [vals, setVals] = useState<Record<string, string>>(() =>
    Object.fromEntries(fields.filter((f) => f.default != null).map((f) => [f.key, f.default as string])));
  const [sandbox, setSandbox] = useState(!!p?.sandbox);
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);
  async function go() {
    setBusy(true); setErr(null);
    try {
      const r = target === "alpaca_paper" ? await api.post("/api/broker/connect", { key_id: vals.key_id, secret: vals.secret })
        : target === "alpaca_live" ? await api.post("/api/live-gate/keys", { key_id: vals.key_id, secret: vals.secret })
        : await api.post("/api/linked", { provider: target, sandbox, values: vals });
      notify(target === "alpaca_paper" ? "Alpaca paper connected." : target === "alpaca_live" ? "Alpaca live keys checked and saved." : `${p?.name ?? "Account"} connected.`);
      onDone(r);
    } catch (e) { setErr((e as ApiError).message); } finally { setBusy(false); }
  }
  return (
    <form className="portal-form" onSubmit={(e) => { e.preventDefault(); go(); }}>
      {p?.help && <p className="small muted">{p.help}</p>}
      {target === "alpaca_paper" && <p className="small muted">Alpaca dashboard → Paper trading → API keys. Checked with a read-only call, then stored encrypted.</p>}
      {target === "alpaca_live" && <p className="small muted">Alpaca dashboard → Live trading → API keys. Checked with a read-only call, then stored encrypted.</p>}
      {fields.map((f) => (
        <label key={f.key}>{f.label}
          <input type={f.secret ? "password" : "text"} autoComplete="off" spellCheck={false} placeholder={f.placeholder ?? ""} value={vals[f.key] ?? f.default ?? ""}
            onChange={(e) => setVals({ ...vals, [f.key]: e.target.value })} />
        </label>
      ))}
      {p?.sandbox && <label className="inline"><input type="checkbox" checked={sandbox} onChange={(e) => setSandbox(e.target.checked)} /> Testnet / demo account</label>}
      {err && <p className="small loss">{err}</p>}
      <button className="btn primary" disabled={busy}>{busy ? "Checking…" : "Check and connect"}</button>
    </form>
  );
}

/** Real money on this account, right after connecting: one typed confirmation per account, then orders can go live. */
function GoLive({ item, notify, onChanged }: { item: Item; notify: (s: string) => void; onChanged: () => void }) {
  const alpaca = item.id === "alpaca";
  const liveKeys = item.methods.find((m) => m.target === "alpaca_live")?.connected;
  const realAccounts = item.methods.flatMap((m) => m.accounts ?? []).filter((a) => !a.sandbox);
  const [acct, setAcct] = useState<string | null>(realAccounts[0]?.id ?? null);
  const [st, setSt] = useState<any>(null);
  const [phrase, setPhrase] = useState("");
  const [busy, setBusy] = useState(false);
  const [rev, setRev] = useState(0);
  useEffect(() => {
    if (alpaca && liveKeys) api.get<any>("/api/live-gate").then(setSt).catch(() => {});
    else if (acct) api.get<any>(`/api/linked/${acct}/live`).then(setSt).catch(() => {});
  }, [alpaca, liveKeys, acct, rev]);
  if (alpaca && !liveKeys) return <KeyForm target="alpaca_live" providers={[]} notify={notify} onDone={onChanged} />;
  if (!alpaca && !realAccounts.length) return <p className="small muted">Real-money trading needs a non-testnet account connected here first.</p>;
  if (!st) return <p className="small muted">Checking the account…</p>;
  const on = alpaca ? !!st.enabled : !!st.enabled_at;
  const want = alpaca ? st.confirm_phrase : st.phrase;
  const blockers = (st.checks ?? []).filter((c: any) => c.ok === false);
  async function enable() {
    setBusy(true);
    try {
      if (alpaca) await api.post("/api/live-gate/enable", { phrase, broker: "alpaca" });
      else await api.put(`/api/linked/${acct}/live`, { on: true, phrase });
      notify(`Real-money trading is on for ${item.name}. Every order still asks you to type its ticker.`);
      setPhrase(""); setRev((r) => r + 1); onChanged();
    } catch (e) { notify((e as ApiError).message); } finally { setBusy(false); }
  }
  if (on) return <p className="portal-ok"><Check size={14} aria-hidden /> Real-money trading is on. Switch back to paper any time in the Broker tab.</p>;
  return (
    <div className="portal-live">
      {!alpaca && realAccounts.length > 1 && (
        <label>Account <select value={acct ?? ""} onChange={(e) => setAcct(e.target.value)}>
          {realAccounts.map((a) => <option key={a.id} value={a.id}>{a.label}{a.total_usd != null ? ` · ${money(a.total_usd)}` : ""}</option>)}</select></label>
      )}
      {!alpaca && blockers.length > 0 && <ul className="small portal-checks">{blockers.map((c: any) => <li key={c.name}>{c.name}</li>)}</ul>}
      <label>Type <q>{want}</q> to switch real money on
        <input value={phrase} onChange={(e) => setPhrase(e.target.value)} autoComplete="off" spellCheck={false} /></label>
      <button className="btn danger" disabled={busy || phrase.trim() !== want || (!alpaca && blockers.length > 0)} onClick={enable}>
        <ShieldAlert size={14} aria-hidden /> Turn on real-money trading</button>
    </div>
  );
}

export function ConnectPortal({ item, providers, notify, onClose, onChanged, goTab }: {
  item: Item; providers: Provider[]; notify: (s: string) => void; onClose: () => void; onChanged: () => void; goTab: (side: string) => void;
}) {
  const [form, setForm] = useState<string | null>(null);
  const connected = item.state === "connected";
  const canLive = item.id === "alpaca" || (!!item.trading && item.methods.some((m) => m.accounts?.length));
  const box = useRef<HTMLElement>(null);
  useDialog(box, onClose);

  async function pick(m: Method) {
    if (m.kind === "oauth") {
      if (!m.available) return notify(m.why ?? "Not offered on this server yet.");
      try { const r = await api.get<{ authorize_url: string }>(`/api/oauth/${m.provider}/start`); location.href = r.authorize_url; }
      catch (e) { notify((e as ApiError).message); }
      return;
    }
    if (m.target === "ibkr") { onClose(); return goTab("broker"); }
    if (m.kind === "plan" || m.kind === "local" || m.target === "anthropic") { onClose(); return goTab("copilot"); }
    if (m.kind === "connector" || m.kind === "token") { onClose(); return goTab("account"); }
    if (m.target) setForm(m.target);
  }

  return (
    <motion.div className="modal-scrim" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.16 }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <motion.section ref={box} role="dialog" aria-modal="true" aria-label={`Connect ${item.name}`} className="portal card"
        initial={{ opacity: 0, y: 16, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 10, scale: 0.98 }}
        transition={{ type: "spring", stiffness: 420, damping: 34 }}>
        <header className="portal-head">
          <Monogram name={item.name} />
          <div className="grow"><h2>{item.name}</h2><p className="small muted">{item.segment}</p></div>
          <button className="iconbtn" onClick={onClose} aria-label="Close"><X size={16} aria-hidden /></button>
        </header>
        <p className="small">{item.how}</p>
        <ol className="portal-steps">
          <li className={connected ? "done" : "now"}>
            <h3>{connected ? <><Check size={14} aria-hidden /> Connected</> : "Sign in"}</h3>
            {!form && (
              <div className="portal-methods">
                {item.methods.filter((m) => m.target !== "alpaca_live").map((m, i) => {
                  const Icon = KIND_ICON[m.kind] ?? KeyRound;
                  return (
                    <button key={i} className={`portal-method ${m.connected ? "is-on" : ""}`} disabled={!m.available && m.kind !== "oauth"}
                      title={m.why ?? ""} onClick={() => pick(m)}>
                      <Icon size={16} aria-hidden />
                      <span className="grow"><b>{m.label}</b>
                        {!m.available && <small>{m.why ?? "Not offered here"}</small>}
                        {m.detail && <small>{m.detail}</small>}
                        {m.limit != null && m.available && <small>{m.used}/{m.limit} used this {m.period}</small>}
                        {m.accounts?.map((a) => <small key={a.id}>{a.label}{a.live ? " · real money on" : a.sandbox ? " · testnet" : " · connected"}{a.total_usd != null ? ` · ${money(a.total_usd)}` : ""}</small>)}
                      </span>
                      {m.connected && <Check size={14} aria-label="connected" />}
                    </button>
                  );
                })}
                {item.methods.length === 0 && <p className="small muted">Tickmark can't connect to {item.name}: it has no public API for this.</p>}
              </div>
            )}
            {form && <>
              <KeyForm key={form} target={form} providers={providers} notify={notify} onDone={() => { setForm(null); onChanged(); }} />
              <button className="linkish small" onClick={() => setForm(null)}>Other ways to connect</button>
            </>}
          </li>
          {canLive && (
            <li className={connected ? "now" : "later"}>
              <h3>Trade real money</h3>
              {connected ? <GoLive item={item} notify={notify} onChanged={onChanged} />
                : <p className="small muted">Connect first. Then you can switch real-money trading on here in one step.</p>}
            </li>
          )}
        </ol>
        <p className="small muted portal-foot">Keys are checked with a read-only call, stored encrypted for your account only, and never sent back to the browser.</p>
      </motion.section>
    </motion.div>
  );
}
