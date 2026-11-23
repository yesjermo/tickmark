import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import { money } from "./ui";

interface Field { key: string; label: string; secret: boolean; default: string | null; placeholder: string | null }
interface Provider { id: string; kind: string; name: string; sandbox: boolean; fields: Field[]; help: string }
interface Holding { asset: string; quantity: number; available: number; price_usd: number | null; usd_value: number | null; note: string | null }
interface Account { id: string; provider: string; provider_name: string; kind: string; label: string; sandbox: boolean; live_enabled_at: string | null; fields: Record<string, string>;
  snapshot: { holdings: Holding[]; total_usd: number; as_of: string; environment: string } | null; error: string | null }
interface Linked { providers: Provider[]; accounts: Account[]; total_usd_valued: number }

const qty = (n: number) => n.toLocaleString(undefined, { maximumFractionDigits: n < 1 ? 8 : 4 });

const LIVE_PHRASE = "I understand this trades real money";

function LiveSwitch({ a, notify, reload }: { a: Account; notify: (s: string) => void; reload: () => void }) {
  const [phrase, setPhrase] = useState("");
  if (a.sandbox || !["ccxt", "robinhood"].includes(a.kind)) return null;
  async function set(on: boolean) {
    try { await api.put(`/api/linked/${a.id}/live`, { on, phrase }); notify(on ? `REAL-MONEY trading enabled on ${a.label}.` : `${a.label} is read-only again.`); }
    catch (e) { notify((e as ApiError).message); } finally { setPhrase(""); reload(); }
  }
  return a.live_enabled_at ? (
    <div className="live-on small"><b className="loss">REAL MONEY trading ON</b> since {a.live_enabled_at.replace("T", " ").slice(0, 16)} UTC
      <button className="btn small" onClick={() => set(false)}>Turn off</button></div>
  ) : (
    <div className="small col">
      <span className="muted">Real-money trading: off (read-only). To trade this account, type <code>{LIVE_PHRASE}</code></span>
      <div className="row"><input value={phrase} onChange={(e) => setPhrase(e.target.value)} aria-label="Confirmation phrase" />
        <button className="btn small danger" disabled={phrase !== LIVE_PHRASE} onClick={() => set(true)}>Enable</button></div>
    </div>
  );
}

export function LinkedAccounts({ notify }: { notify: (s: string) => void }) {
  const [d, setD] = useState<Linked | null>(null);
  const [prov, setProv] = useState("");
  const [vals, setVals] = useState<Record<string, string>>({});
  const [label, setLabel] = useState("");
  const [sandbox, setSandbox] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const load = () => api.get<Linked>("/api/linked").then(setD).catch((e) => notify((e as ApiError).message));
  useEffect(() => { load(); }, []);
  if (!d) return null;
  const p = d.providers.find((x) => x.id === prov);

  async function add() {
    setBusy("add");
    try {
      await api.post("/api/linked", { provider: prov, label, sandbox, values: vals });
      notify("Linked. Keys are in the system keychain; SwingDesk only reads this account.");
      setProv(""); setVals({}); setLabel(""); load();
    } catch (e) { notify((e as ApiError).message); } finally { setBusy(null); }
  }
  async function act(id: string, fn: () => Promise<unknown>) {
    setBusy(id);
    try { await fn(); } catch (e) { notify((e as ApiError).message); } finally { setBusy(null); load(); }
  }

  return (
    <section className="linked">
      <h4>Linked accounts <span className="badge">read-only</span></h4>
      <p className="small muted">Exchanges, brokers and wallets SwingDesk only reads (balances and positions), so the copilot and your
        review see everything in one place. Orders still go only through the trading broker above.</p>
      {d.accounts.length > 0 && <p className="small">Priced total: <b>{money(d.total_usd_valued)}</b></p>}
      {d.accounts.map((a) => (
        <details key={a.id} className="card linked-acc">
          <summary className="row between">
            <span><b>{a.label}</b> <span className="small muted">{a.provider_name}</span></span>
            <span className="num small">{a.snapshot ? money(a.snapshot.total_usd) : "—"}</span>
          </summary>
          {a.snapshot && <p className="small muted">{a.snapshot.environment} · as of {a.snapshot.as_of.replace("T", " ").slice(0, 16)} UTC</p>}
          {a.error && <p className="small loss">{a.error}</p>}
          {a.snapshot && (
            <table className="mini"><thead><tr><th>Asset</th><th>Qty</th><th>USD</th></tr></thead>
              <tbody>{a.snapshot.holdings.map((h, i) => (
                <tr key={i} title={h.note ?? ""}><td>{h.asset}</td><td className="num">{qty(h.quantity)}</td>
                  <td className="num">{h.usd_value != null ? money(h.usd_value) : <span className="muted">—</span>}</td></tr>))}</tbody></table>
          )}
          <LiveSwitch a={a} notify={notify} reload={load} />
          <div className="row">
            <button className="btn small" disabled={busy === a.id} onClick={() => act(a.id, () => api.post(`/api/linked/${a.id}/refresh`))}>Refresh</button>
            <button className="btn small ghost" disabled={busy === a.id}
              onClick={() => window.confirm(`Unlink ${a.label}? Its keys are deleted from the keychain.`) && act(a.id, () => api.del(`/api/linked/${a.id}`))}>Unlink</button>
          </div>
        </details>
      ))}
      <div className="linked-add">
        <select value={prov} onChange={(e) => { setProv(e.target.value); setVals({}); setSandbox(true); }} aria-label="Provider to link">
          <option value="">+ Link an account…</option>
          {d.providers.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
        </select>
        {p && (
          <form className="col small" onSubmit={(e) => { e.preventDefault(); add(); }}>
            <p className="muted">{p.help}</p>
            <label>Name <input value={label} onChange={(e) => setLabel(e.target.value)} placeholder={p.name} /></label>
            {p.fields.map((f) => (
              <label key={f.key}>{f.label}
                {f.key === "secret" && p.id === "coinbase"
                  ? <textarea rows={3} autoComplete="off" value={vals[f.key] ?? ""} onChange={(e) => setVals({ ...vals, [f.key]: e.target.value })} />
                  : <input type={f.secret ? "password" : "text"} autoComplete="off" value={vals[f.key] ?? f.default ?? ""}
                      placeholder={f.placeholder ?? ""} onChange={(e) => setVals({ ...vals, [f.key]: e.target.value })} />}
              </label>
            ))}
            {p.sandbox && <label className="row"><input type="checkbox" checked={sandbox} onChange={(e) => setSandbox(e.target.checked)} />
              {p.kind === "futu" ? "Paper (simulate) account" : "Testnet / demo account"}</label>}
            {(!p.sandbox || !sandbox) && p.kind !== "solana" && <p className="warn">Real account. Linking only reads it; trading needs the real-money switch afterwards
              and a key with trade permission (never withdrawal).</p>}
            <button className="btn primary" disabled={busy === "add"}>{busy === "add" ? "Checking…" : "Check and link"}</button>
          </form>
        )}
      </div>
    </section>
  );
}
