import { Mark } from "./Mark";
import { useState } from "react";
import { api, ApiError } from "../api";

export interface PortalUser { id: string; email: string; name: string | null; role: string }

export function Login({ firstUser, openSignup, setupCode = false, onIn }: { firstUser: boolean; openSignup: boolean; setupCode?: boolean; onIn: (u: PortalUser) => void }) {
  const [mode, setMode] = useState<"in" | "up">(firstUser ? "up" : "in");
  const [f, setF] = useState({ email: "", password: "", name: "", invite: "" });
  const [err, setErr] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  async function go() {
    setBusy(true); setErr(null);
    try {
      const r = await api.post<{ user: PortalUser }>(mode === "in" ? "/api/auth/login" : "/api/auth/signup",
        mode === "in" ? { email: f.email, password: f.password } : f);
      const next = new URLSearchParams(location.search).get("next");
      if (next && next.startsWith("/oauth/authorize")) { location.href = next; return; }   // back to "Connect <AI app>?"
      onIn(r.user);
    } catch (e) { setErr((e as ApiError).message); } finally { setBusy(false); }
  }
  return (
    <div className="login-wrap">
      <form className="card login" onSubmit={(e) => { e.preventDefault(); go(); }}>
        <h1><span className="logo"><Mark size={18} /></span> Tickmark</h1>
        <p className="muted small">{firstUser ? "Create the administrator account for this Tickmark server." :
          mode === "in" ? "Sign in to your desk." : openSignup ? "Create your account." : "Create your account with the invite code you were sent."}</p>
        <label>Email <input type="email" autoComplete="email" value={f.email} onChange={(e) => setF({ ...f, email: e.target.value })} /></label>
        <label>Password <input type="password" autoComplete={mode === "in" ? "current-password" : "new-password"} value={f.password}
          onChange={(e) => setF({ ...f, password: e.target.value })} /></label>
        {mode === "up" && <>
          <label>Name (optional) <input value={f.name} onChange={(e) => setF({ ...f, name: e.target.value })} /></label>
          {((!firstUser && !openSignup) || (firstUser && setupCode)) && <label>{firstUser ? "Setup code (from the server's settings)" : "Invite code"}
            <input value={f.invite} autoComplete="off" onChange={(e) => setF({ ...f, invite: e.target.value })} /></label>}
          <p className="small muted">At least 10 characters. Your broker and exchange keys are stored encrypted, separately from every other user.</p>
        </>}
        {err && <p className="small loss">{err}</p>}
        <button className="btn primary" disabled={busy || !f.email || !f.password}>{busy ? "…" : mode === "in" ? "Sign in" : "Create account"}</button>
        {!firstUser && <button type="button" className="linkish small" onClick={() => setMode(mode === "in" ? "up" : "in")}>
          {mode === "in" ? "New here? Create an account" : "Have an account? Sign in"}</button>}
      </form>
    </div>
  );
}
