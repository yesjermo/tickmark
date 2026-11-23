import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { PortalUser } from "./Login";
import { AdminAI } from "./AdminAI";
import { GitHubMark, GoogleMark } from "./Mark";
import { Monogram } from "./ConnectPortal";
import { openTradingViewSignIn } from "./TradingView";
import { Check } from "lucide-react";

interface Tok { id: string; name: string; prefix: string; created_at: string; last_used_at: string | null; revoked_at: string | null }

export function AccountPanel({ user, notify, adminUnclaimed = false, social = [] }: { user: PortalUser | null; notify: (s: string) => void;
  adminUnclaimed?: boolean; social?: string[] }) {
  const githubLogin = social.includes("github"), googleLogin = social.includes("google");
  const [code, setCode] = useState("");
  async function claim() {
    try { await api.post("/api/portal/claim-admin", { code }); notify("You're the administrator now."); setTimeout(() => location.reload(), 900); }
    catch (e) { notify((e as ApiError).message); }
  }
  const [toks, setToks] = useState<{ tokens: Tok[]; endpoint: string; tools: string[] } | null>(null);
  const [name, setName] = useState("Claude Code");
  const [fresh, setFresh] = useState<string | null>(null);
  const [invite, setInvite] = useState<{ code: string; email: string | null } | null>(null);
  const [invEmail, setInvEmail] = useState("");
  const [users, setUsers] = useState<any[]>([]);
  const admin = !user || user.role === "admin";
  const load = () => api.get<any>("/api/mcp/tokens").then(setToks).catch((e) => notify((e as ApiError).message));
  useEffect(() => { load(); if (user && user.role === "admin") api.get<any[]>("/api/portal/users").then(setUsers).catch(() => {}); }, []);

  async function create() {
    try { const r = await api.post<{ token: string }>("/api/mcp/tokens", { name }); setFresh(r.token); load(); }
    catch (e) { notify((e as ApiError).message); }
  }
  const cmd = fresh && toks ? `claude mcp add --transport http tickmark ${toks.endpoint} --header "Authorization: Bearer ${fresh}"` : "";
  return (
    <div className="account">
      {user && (
        <section>
          <h3>Account</h3>
          <p className="small">{user.email} · {user.role}</p>
          <button className="btn small" onClick={async () => { await api.post("/api/auth/logout"); location.reload(); }}>Sign out</button>
        </section>
      )}
      {user && user.role !== "guest" && (
        <section>
          <h3>Connected sign-ins</h3>
          <div className="signin-row">
            <GitHubMark size={18} />
            <span className="grow"><b>GitHub</b><small>{user.github ? "Linked: you can sign in with GitHub." : githubLogin ? "Sign in with GitHub next time." : "Not set up on this server yet."}</small></span>
            {githubLogin && !user.github && <a className="btn small" href="/api/auth/github/start?remember=1">Link GitHub</a>}
            {user.github && <span className="conn-pill st-connected"><Check size={12} aria-hidden /> Linked</span>}
          </div>
          <div className="signin-row">
            <GoogleMark size={18} />
            <span className="grow"><b>Google</b><small>{user.google ? "Linked: you can sign in with Google." : googleLogin ? "Sign in with Google next time." : "Not set up on this server yet."}</small></span>
            {googleLogin && !user.google && <a className="btn small" href="/api/auth/google/start?remember=1">Link Google</a>}
            {user.google && <span className="conn-pill st-connected"><Check size={12} aria-hidden /> Linked</span>}
          </div>
          <div className="signin-row">
            <Monogram name="Trading View" />
            <span className="grow"><b>TradingView</b><small>Sign in on TradingView's own page, then open your charts and layouts from the desk.</small></span>
            <button className="btn small" onClick={openTradingViewSignIn}>Sign in</button>
          </div>
        </section>
      )}
      {user && user.role === "user" && adminUnclaimed && (
        <section>
          <h3>Become administrator</h3>
          <p className="small muted">This server has no administrator yet. The owner enters the setup code from the server's settings
            (Render → Environment → SWINGDESK_SETUP_CODE) once; signing up never needs it.</p>
          <div className="row"><input type="password" autoComplete="off" value={code} onChange={(e) => setCode(e.target.value)} aria-label="Setup code" placeholder="Setup code" />
            <button className="btn small primary" disabled={!code.trim()} onClick={claim}>Claim</button></div>
        </section>
      )}
      <section>
        <h3>Connect AI apps (MCP)</h3>
        <p className="small muted">Claude Code, Claude Desktop and other MCP apps can connect to your desk with a personal token. They get the
          same tools as the copilot: they can read your desk and draft proposals, which wait here for you. They can't place, cancel or close orders.</p>
        <div className="row"><input value={name} onChange={(e) => setName(e.target.value)} aria-label="Token name" />
          <button className="btn small primary" onClick={create}>Create token</button></div>
        {fresh && (
          <div className="card pad small">
            <p><b>Copy it now; it won't be shown again.</b></p>
            <code className="secret">{fresh}</code>
            <p>Claude Code:</p><code className="secret">{cmd}</code>
            <p className="muted">Other apps: MCP server URL {toks?.endpoint}, header Authorization: Bearer &lt;token&gt;.</p>
            <button className="btn small" onClick={() => { navigator.clipboard?.writeText(cmd); notify("Command copied."); }}>Copy command</button>
          </div>
        )}
        {toks && toks.tokens.length > 0 && (
          <table className="mini"><thead><tr><th>Name</th><th>Token</th><th>Last used</th><th></th></tr></thead>
            <tbody>{toks.tokens.map((t) => (
              <tr key={t.id} className={t.revoked_at ? "muted" : ""}><td>{t.name}</td><td><code>{t.prefix}…</code></td>
                <td className="small">{t.revoked_at ? "revoked" : t.last_used_at?.replace("T", " ").slice(0, 16) ?? "never"}</td>
                <td>{!t.revoked_at && <button className="btn small ghost" onClick={async () => { await api.del(`/api/mcp/tokens/${t.id}`); load(); }}>Revoke</button>}</td></tr>))}
            </tbody></table>
        )}
      </section>
      {admin && <AdminAI notify={notify} />}
      {user && admin && (
        <section>
          <h3>People</h3>
          <div className="row"><input placeholder="email (optional)" value={invEmail} onChange={(e) => setInvEmail(e.target.value)} />
            <button className="btn small" onClick={async () => { try { setInvite(await api.post("/api/portal/invites", { email: invEmail })); } catch (e) { notify((e as ApiError).message); } }}>Create invite</button></div>
          {invite && <p className="small">Invite code (shown once): <code className="secret">{invite.code}</code></p>}
          <ul className="plain small">{users.map((u) => <li key={u.id}>{u.email} · {u.role}{u.disabled ? " · disabled" : ""}</li>)}</ul>
        </section>
      )}
    </div>
  );
}
