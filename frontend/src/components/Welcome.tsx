import { motion } from "motion/react";
import { ArrowRight, Check, LogIn, X } from "lucide-react";
import { Mark } from "./Mark";

/** First paint while the session loads: the mark in its ink tile on the theme's canvas, nothing else to read. */
export function Splash() {
  return (
    <div className="splash" role="status" aria-label="Loading Tickmark">
      <div className="splash-mark">
        <span className="splash-tile"><svg width="28" height="28" viewBox="0 0 16 16" aria-hidden><path className="splash-tick" d="M15 1v14H1z" /></svg></span>
        <span>Tickmark</span>
      </div>
      <span className="splash-bar" aria-hidden><i /></span>
    </div>
  );
}

const KEY = "tickmark.welcomed";
export const welcomed = () => { try { return localStorage.getItem(KEY) === "1"; } catch { return false; } };
const markWelcomed = () => { try { localStorage.setItem(KEY, "1"); } catch { /* private mode: shows again next visit */ } };

const STEPS = [
  { t: "Say it in plain English", d: "Describe the setup you trade. The copilot drafts rules you can read, edit and save as a version." },
  { t: "See why, not just what", d: "Every symbol on your watchlist shows each rule's threshold, what was observed, and whether it passed." },
  { t: "Paper first, real money on purpose", d: "Paper orders on real prices. Risk checks, a kill switch and a typed phrase stand before any live trade." },
];

// Illustrative example values for the preview, labelled as such on the page.
const RULES: { n: string; o: string; r: "pass" | "fail" }[] = [
  { n: "Close above the 50-day average", o: "165.05 vs 152.10", r: "pass" },
  { n: "Pulled back to the 9-day EMA", o: "within 0.6%", r: "pass" },
  { n: "Volume lighter than the 20-day", o: "1.12× (needs < 1.0×)", r: "fail" },
  { n: "No earnings before the exit", o: "next report in 41 days", r: "pass" },
];

function Preview() {
  return (
    <div className="wl-preview" aria-label="Illustrative preview of the Tickmark desk">
      <div className="wp-card wp-balance">
        <span className="wp-k">Total balance · Paper account</span>
        <b className="num">100,000.00<small>USD</small></b>
        <div className="wp-acts">
          {["Order ticket", "Research", "Journal", "Connect"].map((a) => <span key={a}><i aria-hidden />{a}</span>)}
        </div>
      </div>
      <div className="wp-card wp-evidence">
        <div className="wp-head">
          <b>NVDA</b><span className="chip st-NEAR_MISS">Near miss</span><span className="wp-px num">165.05</span>
        </div>
        <ul>
          {RULES.map((x) => (
            <li key={x.n}>
              <span className={`res res-${x.r}`}>{x.r === "pass" ? "✓ pass" : "✕ fail"}</span>
              <span className="wp-n">{x.n}</span>
              <span className="wp-o num">{x.o}</span>
            </li>
          ))}
        </ul>
        <p className="wp-why">Dropped on one rule: volume. No trade is a fine outcome.</p>
      </div>
      <span className="wp-note">Illustrative example</span>
    </div>
  );
}

/** The front page a first-time visitor meets: what Tickmark is, then in (as a guest) or sign in / sign up. */
export function Welcome({ onEnter, onAuth, guest }: { onEnter: () => void; onAuth: (m: "in" | "up") => void; guest: boolean }) {
  const enter = () => { markWelcomed(); onEnter(); };
  const ease = [0.2, 0.8, 0.2, 1] as const;
  return (
    <motion.div className="welcome" initial={false} animate={{ opacity: 1 }} exit={{ opacity: 0, transition: { duration: 0.18 } }}>
      <header className="wl-top">
        <span className="wl-brand"><span className="wl-logo"><Mark size={15} /></span>Tickmark</span>
        <nav className="wl-nav">
          <button className="btn ghost small" onClick={() => onAuth("in")}><LogIn size={14} aria-hidden /> Sign in</button>
          <button className="iconbtn wl-x" onClick={enter} aria-label="Skip the introduction and open the desk" title="Open the desk"><X size={16} aria-hidden /></button>
        </nav>
      </header>
      <main className="wl-main">
        <motion.section className="wl-copy" initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.5, ease }}>
          <h1>A trading desk that shows its&nbsp;reasons.</h1>
          <p className="wl-lede">Tickmark turns the setup you describe into rules you can read, screens your watchlist on real prices,
            and records every trade in a journal that can't be rewritten. Nothing trades until you click.</p>
          <div className="wl-cta">
            <button className="btn primary wl-go" onClick={enter}>{guest ? "Open the desk" : "Get started"} <ArrowRight size={16} aria-hidden /></button>
            <button className="btn wl-up" onClick={() => onAuth("up")}>Create free account</button>
          </div>
          <p className="wl-fine">No account needed to look around: guests trade on paper with real prices, and signing up keeps it all.</p>
          <ol className="wl-steps">
            {STEPS.map((s) => (
              <li key={s.t}><Check size={15} strokeWidth={2.2} aria-hidden /><div><b>{s.t}</b><span>{s.d}</span></div></li>
            ))}
          </ol>
        </motion.section>
        <motion.div className="wl-show" initial={{ opacity: 0, y: 22 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.6, delay: 0.08, ease }}>
          <Preview />
        </motion.div>
      </main>
      <footer className="wl-foot">Decision-support software, not investment advice. Paper trading by default.</footer>
    </motion.div>
  );
}
