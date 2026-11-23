import { useEffect, useState } from "react";
import { api, ApiError } from "../api";
import type { PortalUser } from "./Login";
import { AdminAI } from "./AdminAI";

interface Tok { id: string; name: string; prefix: string; created_at: string; last_used_at: string | null; revoked_at: string | null }

export function AccountPanel({ user, notify }: { user: PortalUser | null; notify: (s: string) => void }) {
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
