import { useEffect, useRef, useState } from "react";
import { api, ApiError } from "../api";
import type { Level } from "../App";

interface ModelInfo { id: string; name: string; price: string; note: string }
interface Plan { id: string; name: string; enabled: boolean; used: number; limit: number; period: string; model: string; note: string }
interface CopilotStatus { configured: boolean; ready?: boolean; plans?: Plan[]; claude_configured: boolean; source: string | null; key_hint: string | null; model: string;
  model_label: string; effort: string; local_loopback: boolean; models: ModelInfo[];
  settings: { model: string; local_base_url: string; local_model: string };
  threads: { id: string; title: string; model: string | null; updated_at: string }[] }
interface LocalModels { reachable: boolean; base_url: string; models: string[]; loopback: boolean; error?: string }

/** Minimal formatting for model replies: **bold** only; everything else stays plain text (no HTML injection). */
function Rich({ text }: { text: string }) {
  return <>{text.split(/(\*\*[^*]+\*\*)/g).map((part, i) =>
    part.startsWith("**") && part.endsWith("**") && part.length > 4 ? <b key={i}>{part.slice(2, -2)}</b> : <span key={i}>{part}</span>)}</>;
}

function ModelPicker({ st, onSaved, notify }: { st: CopilotStatus; onSaved: (s: CopilotStatus) => void; notify: (s: string) => void }) {
  const [url, setUrl] = useState(st.settings.local_base_url);
  const [name, setName] = useState(st.settings.local_model);
  const [lm, setLm] = useState<LocalModels | null>(null);
  const local = st.model === "local";
  const info = st.models.find((m) => m.id === st.model);
  const plan = st.plans?.find((p) => p.id === st.model);

  async function save(model: string, extra: Record<string, string> = {}) {
    try { onSaved(await api.put<CopilotStatus>("/api/copilot/settings", { model, ...extra })); }
    catch (e) { notify((e as ApiError).message); }
  }
  async function check() {
    const r = await api.get<LocalModels>(`/api/copilot/local-models?base_url=${encodeURIComponent(url)}`);
    setLm(r);
    if (r.reachable && r.models.length && !r.models.includes(name)) setName(r.models[0]);
  }
  useEffect(() => { if (local) check(); }, [local]);

  return (
    <div className="modelpick">
      <label className="small muted">Model
        <select value={st.model} onChange={(e) => save(e.target.value)}>
          <optgroup label="Included on this server">
            {(st.plans ?? []).map((p) => <option key={p.id} value={p.id} disabled={!p.enabled}>
              {p.name} ({p.model}){p.enabled ? ` · ${p.limit - p.used} of ${p.limit} left this ${p.period}` : " · not offered here"}</option>)}
          </optgroup>
          <optgroup label="Claude with your own Anthropic key (unlimited)">
            {st.models.map((m) => <option key={m.id} value={m.id}>{m.name} · {m.price} per M tokens</option>)}
          </optgroup>
          <optgroup label="Your own model server (free)">
            <option value="local">Local model: {st.settings.local_model}</option>
          </optgroup>
        </select>
      </label>
      {info && <p className="small muted">{info.note}{!st.claude_configured && " Add your key below to use it."}</p>}
      {plan && (
        <div className="usage" aria-label={`${plan.used} of ${plan.limit} messages used`}>
          <div className="usage-bar"><span style={{ width: `${Math.min(100, (plan.used / Math.max(1, plan.limit)) * 100)}%` }} /></div>
          <span className="small muted">{plan.used} of {plan.limit} messages used this {plan.period}. {plan.note}.
            {plan.id === "free" && " For unlimited use, pick a Claude model with your own key, or the open-source model."}</span>
        </div>
      )}
      {local && (
        <div className="col small">
          <p className="muted">Any OpenAI-compatible server works: Ollama (default), LM Studio (http://localhost:1234/v1), llama.cpp or vLLM.
            Small models (4–9B) answer simple questions well but are weaker at multi-step decisions than Claude; the guardrails are the same.</p>
          <label>Server URL <input value={url} onChange={(e) => setUrl(e.target.value)} onBlur={check} /></label>
          <label>Model name
            {lm?.reachable && lm.models.length > 0
              ? <select value={name} onChange={(e) => setName(e.target.value)}>{lm.models.map((m) => <option key={m}>{m}</option>)}
                  {!lm.models.includes(name) && <option>{name}</option>}</select>
              : <input value={name} onChange={(e) => setName(e.target.value)} placeholder="qwen3.5:9b" />}
          </label>
          <div className="row">
            <button className="btn small" onClick={check}>Check server</button>
            <button className="btn small primary" onClick={() => save("local", { local_base_url: url, local_model: name })}
              disabled={url === st.settings.local_base_url && name === st.settings.local_model}>Use this model</button>
          </div>
          {lm && (lm.reachable
            ? <span className={lm.models.includes(name) ? "gain" : "warn"}>
                {lm.models.includes(name) ? `✓ ${name} is available` : `${name} isn't installed; run: ollama pull ${name}`}</span>
            : <span className="loss">Server not reachable. Start Ollama (or LM Studio) first.</span>)}
          {lm && !lm.loopback && <span className="warn">This server isn't on this Mac: questions and desk data are sent to it.</span>}
        </div>
      )}
    </div>
  );
}
interface Proposal { id: string; kind: "rules" | "risk_limits" | "swing_order" | "day_trade" | "exit" | "crypto_order"; title: string; rationale: string;
  payload: any; status: "proposed" | "applied" | "opened" | "dismissed" | "failed"; result: string | null }
interface Step { tool: string; input: any; ok: boolean; error: string | null }
type Turn = { role: "user" | "assistant"; text: string; steps?: Step[]; proposals?: string[] };

const TOOL_LABEL: Record<string, string> = {
  get_overview: "desk overview", get_symbol_evaluation: "rule evidence", get_indicators: "indicators", list_strategies: "strategies",
  get_positions_and_orders: "positions & orders", get_journal: "journal", get_risk: "risk limits", get_forward_test: "forward test",
  get_day_trading: "day trading", propose_rule_change: "draft rule change", propose_risk_limits: "draft risk limits",
  propose_swing_order: "draft order", propose_day_trade: "draft day trade", propose_exit: "draft exit",
};
const KIND_LABEL: Record<Proposal["kind"], string> = { rules: "Rule change", risk_limits: "Risk limits", swing_order: "Swing order",
  day_trade: "Day trade", exit: "Exit", crypto_order: "Crypto order" };

function ProposalCard({ p, onAct }: { p: Proposal; onAct: (p: Proposal, a: "apply" | "dismiss") => void }) {
  const pl = p.payload;
  const open = p.status === "proposed";
  const applyLabel = p.kind === "swing_order" ? "Open in ticket" : p.kind === "day_trade" ? "Open Day trading" : p.kind === "crypto_order" ? "Open Crypto tab"
    : p.kind === "exit" ? `Send ${pl.action}` : "Apply";
  return (
    <div className={`card proposal pr-${p.status}`}>
      <div className="row between"><span className="kind-tag">{KIND_LABEL[p.kind]}</span><span className="small muted">{p.status}</span></div>
      <b>{p.title}</b>
      {p.rationale && <p className="small">{p.rationale}</p>}
      {(p.kind === "rules" || p.kind === "risk_limits") && (
        <ul className="plain small num">{Object.entries(pl.changes as Record<string, { from: any; to: any }>).map(([k, v]) =>
          <li key={k}>{k}: {String(v.from ?? "—")} → <b>{Array.isArray(v.to) ? v.to.join(", ") : String(v.to)}</b></li>)}
          {p.kind === "rules" && pl.strategy && <li>strategy: {pl.strategy}</li>}</ul>
      )}
      {p.kind === "crypto_order" && (
        <p className="small num">{pl.amount} · ~{pl.price} · value {pl.est_cost}{pl.stop_price ? ` · stop ${pl.stop_price}` : ""} · {pl.environment}
          {pl.blocked && <span className="loss"> · blocked by checks</span>}</p>
      )}
      {(p.kind === "swing_order" || p.kind === "day_trade") && (
        <p className="small num">{pl.qty} sh · entry ~{pl.entry_assumption} · stop {pl.stop_price}{pl.target ? ` · target ${pl.target}` : ""} ·
          loss if stopped ${pl.est_loss_if_stopped}
          {pl.risk?.blocked && <span className="loss"> · blocked by risk checks</span>}
          {pl.route && <span className="muted"> · {pl.route.label}</span>}</p>
      )}
      {p.result && <p className="small muted">{p.result}</p>}
      {open && (
        <div className="row">
          <button className="btn small primary" onClick={() => onAct(p, "apply")}>{applyLabel}</button>
          <button className="btn small ghost" onClick={() => onAct(p, "dismiss")}>Dismiss</button>
        </div>
      )}
      {open && (p.kind === "swing_order" || p.kind === "day_trade" || p.kind === "crypto_order") && <p className="small muted">You confirm the order there; the copilot can't send it.</p>}
    </div>
  );
}

export function Copilot({ level, notify, onChanged, openTicket, openDay, openCrypto }: {
  level: Level; notify: (s: string) => void; onChanged: () => void;
  openTicket: (symbol: string, overrides: Record<string, any>) => void; openDay: () => void; openCrypto: (form: any) => void;
}) {
  const [st, setSt] = useState<CopilotStatus | null>(null);
  const [key, setKey] = useState("");
  const [tid, setTid] = useState<string | null>(null);
  const [turns, setTurns] = useState<Turn[]>([]);
  const [props, setProps] = useState<Record<string, Proposal>>({});
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [waiting, setWaiting] = useState<(Proposal & { source: string })[]>([]);
  const end = useRef<HTMLDivElement>(null);
  const loadWaiting = () => api.get<(Proposal & { source: string })[]>("/api/copilot/proposals").then(setWaiting).catch(() => {});
  useEffect(() => { loadWaiting(); }, [turns.length]);

  const load = () => api.get<CopilotStatus>("/api/copilot").then(setSt).catch((e) => notify((e as ApiError).message));
  useEffect(() => { load(); }, []);
  useEffect(() => { end.current?.scrollIntoView({ block: "end" }); }, [turns, busy]);

  async function saveKey() {
    setBusy(true);
    try { await api.put("/api/copilot/key", { key }); setKey(""); notify("Key checked and saved to the system keychain."); load(); }
    catch (e) { notify((e as ApiError).message); } finally { setBusy(false); }
  }
  async function openThread(id: string) {
    const t = await api.get<{ turns: Turn[]; proposals: Proposal[] }>(`/api/copilot/threads/${id}`);
    setTid(id); setTurns(t.turns);
    setProps(Object.fromEntries(t.proposals.map((p) => [p.id, p])));
    if (t.proposals.length) setTurns([...t.turns, { role: "assistant", text: "", proposals: t.proposals.map((p) => p.id) }]);
  }
  async function send(q?: string) {
    const msg = (q ?? text).trim();
    if (!msg || busy) return;
    setText(""); setBusy(true);
    setTurns((t) => [...t, { role: "user", text: msg }]);
    try {
      const r = await api.post<{ thread_id: string; reply: string; steps: Step[]; proposals: Proposal[] }>("/api/copilot/chat",
        { text: msg, thread_id: tid, level });
      setTid(r.thread_id);
      setProps((p) => ({ ...p, ...Object.fromEntries(r.proposals.map((x) => [x.id, x])) }));
      setTurns((t) => [...t, { role: "assistant", text: r.reply, steps: r.steps, proposals: r.proposals.map((x) => x.id) }]);
      if (!tid) load();
    } catch (e) {
      setTurns((t) => [...t, { role: "assistant", text: `⚠ ${(e as ApiError).message}` }]);
    } finally { setBusy(false); }
  }
  async function act(p: Proposal, a: "apply" | "dismiss") {
    if (a === "apply" && (p.kind === "rules" || p.kind === "risk_limits" || p.kind === "exit")
      && !window.confirm(`${p.title}\n\nApply this now? It is logged as a copilot proposal you approved.`)) return;
    try {
      const r = await api.post<any>(`/api/copilot/proposals/${p.id}/${a}`);
      const np: Proposal = r.proposal ?? r;
      setProps((m) => ({ ...m, [np.id]: np }));
      loadWaiting();
      if (r.open === "ticket") openTicket(r.symbol, r.overrides ?? {});
      else if (r.open === "day") openDay();
      else if (r.open === "crypto") openCrypto(r.form);
      else if (a === "apply") { notify(`Done: ${np.result}`); onChanged(); }
    } catch (e) { notify((e as ApiError).message); }
  }

  if (!st) return <div className="pad muted">Loading…</div>;
  const needsKey = !st.ready && !st.claude_configured && st.models.some((m) => m.id === st.model);
  if (!st.configured || needsKey) return (
    <div className="copilot">
      <h3>Copilot</h3>
      <ModelPicker st={st} onSaved={setSt} notify={notify} />
      <p className="small">Ask about your setups, positions and risk in plain English. It reads the desk through the same code the app
        uses, and can draft rule changes, risk limits, orders and exits. <b>Nothing happens until you apply or confirm it.</b></p>
      <p className="small muted">Uses Claude ({st.model}) with your own Anthropic API key, billed by Anthropic per use. Questions and the desk
        data the copilot looks up (watchlist, positions, journal, limits) are sent to Anthropic's API; broker keys are never sent.</p>
      <form onSubmit={(e) => { e.preventDefault(); saveKey(); }} className="col">
        <input type="password" autoComplete="off" placeholder="Anthropic API key (sk-ant-…)" value={key} onChange={(e) => setKey(e.target.value)} />
        <button className="btn primary" disabled={busy || !key.trim()}>{busy ? "Checking…" : "Check and save key"}</button>
      </form>
      <p className="small muted">Stored in the system keychain, never shown again. Or set ANTHROPIC_API_KEY before starting the server.
        No key? Pick an included plan or a local model above.</p>
    </div>
  );

  const examples = ["What qualifies today, and why?", "Review my open positions and their stops",
    "Is my risk per trade sensible for this account?", "Which strategy fits NVDA right now?"];
  return (
    <div className="copilot">
      <div className="row between">
        <h3>Copilot</h3>
        <div className="row">
          {st.threads.length > 0 && (
            <select aria-label="Past conversations" value={tid ?? ""} onChange={(e) => e.target.value ? openThread(e.target.value) : (setTid(null), setTurns([]))}>
              <option value="">New conversation</option>
              {st.threads.map((t) => <option key={t.id} value={t.id}>{t.model?.startsWith("local:") ? "◆ " : ""}{t.title.slice(0, 40)}</option>)}
            </select>
          )}
          {tid && <button className="btn small ghost" onClick={() => { setTid(null); setTurns([]); }}>New</button>}
        </div>
      </div>
      <ModelPicker st={st} notify={notify} onSaved={(n) => {
        const fam = (m: string) => (m === "local" ? "local" : "claude");
        if (tid && fam(n.model) !== fam(st.model)) { setTid(null); setTurns([]); notify("Switched model family; started a new conversation."); }
        setSt({ ...n, threads: st.threads });
      }} />
      {waiting.filter((p) => !props[p.id]).length > 0 && (
        <details className="waiting" open>
          <summary className="small"><b>Waiting for you ({waiting.filter((p) => !props[p.id]).length})</b> <span className="muted">incl. proposals from connected AI apps</span></summary>
          {waiting.filter((p) => !props[p.id]).map((p) => <div key={p.id}><span className="small muted">{p.source === "mcp" ? "from an AI app (MCP)" : "from the copilot"}</span>
            <ProposalCard p={p} onAct={act} /></div>)}
        </details>
      )}
      <div className="chat">
        {turns.length === 0 && (
          <div className="small muted">
            <p>Reads your desk and drafts proposals. You apply or confirm everything.</p>
            <div className="chips">{examples.map((q) => <button key={q} className="chip" onClick={() => send(q)}>{q}</button>)}</div>
          </div>
        )}
        {turns.map((t, i) => (
          <div key={i} className={`turn ${t.role}`}>
            {t.steps && t.steps.length > 0 && (
              <div className="steps">{t.steps.map((s, j) => (
                <span key={j} className={`step ${s.ok ? "" : "err"}`} title={s.error ?? JSON.stringify(s.input)}>
                  {s.ok ? "✓" : "✕"} {TOOL_LABEL[s.tool] ?? s.tool}{s.input?.symbol ? ` ${s.input.symbol}` : ""}</span>))}</div>
            )}
            {t.text && <div className="bubble">{t.role === "assistant" ? <Rich text={t.text} /> : t.text}</div>}
            {t.proposals?.map((id) => props[id] && <ProposalCard key={id} p={props[id]} onAct={act} />)}
          </div>
        ))}
        {busy && <div className="turn assistant"><div className="bubble muted">{st.model === "local" ? "Local model working… (can take a minute)" : "Looking it up…"}</div></div>}
        <div ref={end} />
      </div>
      <form className="ask" onSubmit={(e) => { e.preventDefault(); send(); }}>
        <textarea rows={2} value={text} placeholder="Ask about setups, positions, risk…" onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); } }} />
        <button className="btn primary" disabled={busy || !text.trim()}>Ask</button>
      </form>
      <p className="small muted">{st.model_label}
        {st.model !== "local" && <> · key {st.source === "keychain" ? st.key_hint : "from environment"}</>}
        {st.model !== "local" && st.source === "keychain" && <> · <button className="linkish" onClick={async () => { await api.del("/api/copilot/key"); load(); }}>remove key</button></>}
        {st.model === "local" && (st.local_loopback ? " · runs on this Mac; nothing leaves it" : " · remote server")}
        {" "}· decision support, not financial advice.</p>
    </div>
  );
}
