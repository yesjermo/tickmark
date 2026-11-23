import { useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Check, Sparkles } from "lucide-react";
import { api, ApiError } from "../api";

interface Proposal { id: string; kind: string; title: string; rationale: string; payload: any; status: string; result: string | null }

const IDEAS = ["Momentum pullbacks in semiconductors with earnings safety", "Conservative mean reversion for large caps", "Breakouts on heavy volume, tight stops"];

/** The copilot researches a strategy for the user's watchlist and drafts it as a versioned rule set. Nothing changes until "Use these rules". */
export function StrategyResearch({ onApplied, notify }: { onApplied: () => void; notify: (s: string) => void }) {
  const [goal, setGoal] = useState("");
  const [busy, setBusy] = useState(false);
  const [reply, setReply] = useState<string | null>(null);
  const [props, setProps] = useState<Proposal[]>([]);
  async function research() {
    setBusy(true); setReply(null); setProps([]);
    const text = `Research a swing strategy for my watchlist. Goal: ${goal.trim()}.
Steps: call list_strategies, then get_overview, then get_indicators for the watchlist symbols that matter for this goal, and get_forward_test.
Pick the library strategy and parameters that fit the goal and today's data, explain the evidence in a few plain sentences (what each rule
filters out and why), name the main risk, then draft it with propose_rule_change. Don't draft orders.`;
    try {
      const r = await api.post<{ reply: string; proposals: Proposal[] }>("/api/copilot/chat", { text });
      setReply(r.reply);
      setProps(r.proposals.filter((p) => p.kind === "rules"));
    } catch (e) { notify((e as ApiError).message); } finally { setBusy(false); }
  }
  async function apply(p: Proposal) {
    try {
      await api.post(`/api/copilot/proposals/${p.id}/apply`);
      setProps((xs) => xs.map((x) => (x.id === p.id ? { ...x, status: "applied" } : x)));
      notify("New rule version saved. The watchlist was re-screened with it.");
      onApplied();
    } catch (e) { notify((e as ApiError).message); }
  }
  return (
    <section className="research">
      <h3><Sparkles size={15} aria-hidden /> Research a strategy with AI</h3>
      <p className="small muted">Your copilot model studies your watchlist, indicators and the strategy library, then drafts rules you can read.
        Applied rules screen your live watchlist; on an account with real money switched on, qualifying setups go to the ticket for a live order.</p>
      <textarea rows={2} value={goal} placeholder="What should the strategy do? e.g. buy quiet pullbacks in strong trends, avoid earnings"
        onChange={(e) => setGoal(e.target.value)} />
      <div className="research-ideas">
        {IDEAS.map((i) => <button key={i} className="chip-btn" onClick={() => setGoal(i)}>{i}</button>)}
      </div>
      <button className="btn primary" disabled={busy || goal.trim().length < 6} onClick={research}>
        {busy ? "Researching… (reads your data first)" : "Research and draft rules"}</button>
      <AnimatePresence>
        {reply && (
          <motion.div className="research-out" initial={{ opacity: 0, y: 6 }} animate={{ opacity: 1, y: 0 }} exit={{ opacity: 0 }}>
            <p className="pre small">{reply}</p>
            {props.map((p) => (
              <div key={p.id} className="research-prop">
                <b>{p.title}</b>
                {p.rationale && <p className="small muted">{p.rationale}</p>}
                {p.status === "applied" ? <span className="portal-ok"><Check size={14} aria-hidden /> In use: these rules now screen your watchlist</span>
                  : <button className="btn primary small" onClick={() => apply(p)}>Use these rules</button>}
              </div>
            ))}
            {props.length === 0 && <p className="small muted">No rule draft came back. Models in snapshot mode can't draft; pick Claude, GPT, Gemini or Gemma in the Copilot tab.</p>}
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  );
}
