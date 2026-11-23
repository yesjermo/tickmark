import { useEffect, useState } from "react";
import { api, ApiError } from "../api";

interface KeyState { hint: string | null; source: "environment" | "app" | null }
interface State { free_tier: boolean; free_tier_source: string | null; keys: Record<string, KeyState>; oss_base_url: string | null;
  oss_models: string | null; plans: { id: string; name: string; enabled: boolean; note: string }[] }

const KEYS: { id: string; label: string; where: string }[] = [
  { id: "GEMINI_API_KEY", label: "Google Gemini", where: "aistudio.google.com (has a free tier)" },
  { id: "OPENAI_API_KEY", label: "OpenAI GPT", where: "platform.openai.com" },
  { id: "ANTHROPIC_API_KEY", label: "Anthropic Claude", where: "console.anthropic.com" },
  { id: "SWINGDESK_OSS_API_KEY", label: "Open-model service (Groq, OpenRouter…)", where: "only for a hosted service; not needed for Ollama" },
];

/** Admin: give everyone free copilot plans by adding the server's own AI keys. */
export function AdminAI({ notify }: { notify: (s: string) => void }) {
  const [st, setSt] = useState<State | null>(null);
  const [vals, setVals] = useState<Record<string, string>>({});
  const [oss, setOss] = useState({ url: "", models: "" });
  const [busy, setBusy] = useState(false);
  const load = () => api.get<State>("/api/admin/ai").then((s) => { setSt(s); setOss({ url: s.oss_base_url ?? "", models: s.oss_models ?? "" }); })
    .catch((e) => notify((e as ApiError).message));
  useEffect(() => { load(); }, []);
  async function save(body: Record<string, unknown>, msg: string) {
    setBusy(true);
    try { setSt(await api.put<State>("/api/admin/ai", body)); setVals({}); notify(msg); } catch (e) { notify((e as ApiError).message); } finally { setBusy(false); }
  }
  if (!st) return null;
  return (
    <section className="admin-ai">
      <h3>Free AI plans (paid by this server)</h3>
      <p className="small muted">Like an IDE copilot's free tier: signed-in users get a monthly allowance on the keys you add here, with no key of
        their own. Keys are checked with a free call, stored encrypted, and never shown again.</p>
      <label className="row small"><input type="checkbox" checked={st.free_tier} disabled={busy || st.free_tier_source === "environment"}
        onChange={(e) => save({ free_tier: e.target.checked }, e.target.checked ? "Free plans on." : "Free plans off.")} />
        Offer free plans to signed-in users{st.free_tier_source === "environment" && " (set in the server's environment)"}</label>
      {KEYS.map((k) => {
        const s = st.keys[k.id];
        return (
          <form key={k.id} className="col small" onSubmit={(e) => { e.preventDefault(); save({ keys: { [k.id]: vals[k.id] } }, `${k.label} key checked and saved.`); }}>
            <span><b>{k.label}</b> <span className="muted">· {s.hint ? `${s.hint} (${s.source === "environment" ? "environment" : "saved"})` : "not set"} · {k.where}</span></span>
            {s.source !== "environment" && (
              <div className="row">
                <input type="password" autoComplete="off" placeholder={s.hint ? "Replace key" : "Paste key"} value={vals[k.id] ?? ""}
                  onChange={(e) => setVals({ ...vals, [k.id]: e.target.value })} aria-label={`${k.label} key`} />
                <button className="btn small" disabled={busy || !(vals[k.id] ?? "").trim()}>Save</button>
                {s.hint && <button type="button" className="btn small ghost" disabled={busy} onClick={() => save({ keys: { [k.id]: null } }, `${k.label} key removed.`)}>Remove</button>}
              </div>
            )}
          </form>
        );
      })}
      <form className="col small" onSubmit={(e) => { e.preventDefault(); save({ oss_base_url: oss.url, oss_models: oss.models }, "Open-model server saved."); }}>
        <b>Open-model server</b>
        <input placeholder="http://localhost:11434/v1 (Ollama) or https://api.groq.com/openai/v1" value={oss.url} onChange={(e) => setOss({ ...oss, url: e.target.value })} aria-label="Open-model server URL" />
        <input placeholder="Models, comma-separated: gemma4:e2b,gemma4:12b,qwen3.5:4b" value={oss.models} onChange={(e) => setOss({ ...oss, models: e.target.value })} aria-label="Open models" />
        <button className="btn small" disabled={busy}>Save open-model server</button>
      </form>
      <ul className="plain small">{st.plans.map((p) => <li key={p.id} className={p.enabled ? "gain" : "muted"}>{p.enabled ? "On" : "Off"} · {p.name}: {p.note}</li>)}</ul>
    </section>
  );
}
