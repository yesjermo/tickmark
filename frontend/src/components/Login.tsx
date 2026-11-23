import { GitHubMark, Mark } from "./Mark";
import { useRef, useState } from "react";
import { useDialog } from "./theme";
const noop = () => {};
import { motion } from "motion/react";
import { X } from "lucide-react";
import { api, ApiError } from "../api";

export interface PortalUser { id: string; email: string; name: string | null; role: string; github?: boolean }

export function Login({ firstUser, openSignup, githubLogin = false, onIn, start = "in", guest = false, onClose }: {
  firstUser: boolean; openSignup: boolean; githubLogin?: boolean; onIn: (u: PortalUser) => void;
  start?: "in" | "up"; guest?: boolean; onClose?: () => void;
}) {
  const [mode, setMode] = useState<"in" | "up">(firstUser ? "up" : start);
  const [f, setF] = useState({ email: "", password: "", name: "", invite: "" });
  const [remember, setRemember] = useState(true);
  const box = useRef<HTMLDivElement>(null);
  useDialog(box, onClose ?? noop, !!onClose);
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function go() {
    setBusy(true); setErr(null);
    try {
      const r = await api.post<{ user: PortalUser }>(mode === "in" ? "/api/auth/login" : "/api/auth/signup",
        mode === "in" ? { email: f.email, password: f.password, remember } : { ...f, remember });
      const next = new URLSearchParams(location.search).get("next");
      if (next && next.startsWith("/oauth/authorize")) { location.href = next; return; }   // back to "Connect <AI app>?"
      onIn(r.user);
    } catch (e) { setErr((e as ApiError).message); } finally { setBusy(false); }
  }
  const form = (
      <form className="card login" onSubmit={(e) => { e.preventDefault(); go(); }}>
        {onClose && <button type="button" className="iconbtn login-x" onClick={onClose} aria-label="Close"><X size={16} aria-hidden /></button>}
        <h1><span className="logo"><Mark size={18} /></span> Tickmark</h1>
        <p className="muted small">{firstUser ? "Create the administrator account for this Tickmark server." :
          mode === "in" ? "Sign in to your desk." : guest ? "Create a free account. Your guest paper trades, watchlist and journal come with you."
          : openSignup ? "Create your account." : "Create your account with the invite code you were sent."}</p>
        {githubLogin && <>
          <a className="btn gh-btn" href={`/api/auth/github/start?remember=${remember ? 1 : 0}`}><GitHubMark size={16} /> Continue with GitHub</a>
          <div className="or-sep" aria-hidden><span>or with email</span></div>
        </>}
        <label>Email <input type="email" autoComplete="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></label>
        <label>Password <input type="password" autoComplete={mode === "in" ? "current-password" : "new-password"} value={f.password}
          onChange={(e) => setF({ ...f, password: e.target.value })} /></label>
        {mode === "up" && <>
          <label>Name (optional) <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
          {!firstUser && !openSignup && <label>Invite code
            <input value={f.invite} autoComplete="off" onChange={(e) => setF({ ...f, invite: e.target.value })} /></label>}
          <p className="small muted">At least 10 characters. Your broker and exchange keys are stored encrypted, separately from every other user.</p>
        </>}
        <label className="remember"><input type="checkbox" checked={remember} onChange={(e) => setRemember(e.target.checked)} />
          <span>Remember me on this browser <small>for 30 days. Untick on a shared computer.</small></span></label>
        {err && <p className="small loss">{err}</p>}
        <button className="btn primary" disabled={busy || !f.email || !f.password}>{busy ? "…" : mode === "in" ? "Sign in" : "Create account"}</button>
        {!firstUser && <button type="button" className="linkish small" onClick={() => setMode(mode === "in" ? "up" : "in")}>
          {mode === "in" ? "New here? Create an account" : "Have an account? Sign in"}</button>}
      </form>
  );
  if (!onClose) return <div className="login-wrap">{form}</div>;
  return (
    <motion.div className="modal-scrim" initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }} transition={{ duration: 0.16 }}
      onMouseDown={(e) => { if (e.target === e.currentTarget) onClose(); }}>
      <motion.div ref={box} role="dialog" aria-modal="true" aria-label={mode === "in" ? "Sign in" : "Create account"} className="modal-wrap"
        initial={{ opacity: 0, y: 14, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }} exit={{ opacity: 0, y: 8, scale: 0.98 }}
        transition={{ type: "spring", stiffness: 420, damping: 34 }}>
        {form}
      </motion.div>
    </motion.div>
  );
}
