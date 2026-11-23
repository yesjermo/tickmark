import { useEffect, useState } from "react";
import { api, ApiError } from "../api";

interface Alert { id: number; at: string; kind: string; severity: "info" | "warn" | "critical"; title: string; body: string; symbol: string | null; read: number; delivered: Record<string, string> }
interface Settings {
  desktop: boolean; slack: boolean; email: boolean; min_severity: string; email_to: string; smtp_host: string; smtp_port: number;
  smtp_user: string; has_slack_webhook: boolean; has_smtp_password: boolean; desktop_supported: boolean;
}

export function AlertsBell({ tick, notify }: { tick: number; notify: (s: string) => void }) {
  const [open, setOpen] = useState(false);
  const [unread, setUnread] = useState(0);
  useEffect(() => { api.get<{ unread: number }>("/api/alerts").then((r) => setUnread(r.unread)).catch(() => {}); }, [tick, open]);
  return (
    <>
      <button className={`bell ${unread ? "has" : ""}`} onClick={() => setOpen(!open)} aria-label={`Alerts, ${unread} unread`} title="Alerts">
        🔔{unread > 0 && <span className="count">{unread > 99 ? "99+" : unread}</span>}
      </button>
      {open && <AlertsDrawer onClose={() => setOpen(false)} notify={notify} />}
    </>
  );
}

function AlertsDrawer({ onClose, notify }: { onClose: () => void; notify: (s: string) => void }) {
  const [items, setItems] = useState<Alert[]>([]);
  const [s, setS] = useState<Settings | null>(null);
  const [tab, setTab] = useState<"feed" | "settings">("feed");
  const [slackUrl, setSlackUrl] = useState("");
  const [smtpPw, setSmtpPw] = useState("");
  const load = () => api.get<{ alerts: Alert[]; settings: Settings }>("/api/alerts").then((r) => { setItems(r.alerts); setS(r.settings); });
  useEffect(() => { load(); return () => { api.post("/api/alerts/read").catch(() => {}); }; }, []);

  async function save(patch: Partial<Settings>) {
    try { setS(await api.put<Settings>("/api/alerts/settings", { ...s, ...patch })); } catch (e) { notify((e as ApiError).message); }
  }
  async function saveSecret(body: Record<string, string>) {
    try { setS(await api.put<Settings>("/api/alerts/secrets", body)); setSlackUrl(""); setSmtpPw(""); notify("Saved to the system keychain."); }
    catch (e) { notify((e as ApiError).message); }
  }
  async function test(channel: string) {
    try { await api.post("/api/alerts/test", { channel }); notify(`Test sent to ${channel}.`); } catch (e) { notify((e as ApiError).message); }
  }

  return (
    <div className="alerts-drawer" role="dialog" aria-label="Alerts">
      <div className="row between">
        <div className="seg">
          <button className={tab === "feed" ? "on" : ""} onClick={() => setTab("feed")}>Feed</button>
          <button className={tab === "settings" ? "on" : ""} onClick={() => setTab("settings")}>Delivery</button>
        </div>
        <button className="btn small ghost" onClick={onClose}>Close</button>
      </div>
      {tab === "feed" && (
        <ul className="feed">
          {items.length === 0 && <li className="muted small">No alerts yet. New setups, fills, exits, failed fetches and reconciliation breaks show up here.</li>}
          {items.map((a) => (
            <li key={a.id} className={`al al-${a.severity} ${a.read ? "" : "unread"}`}>
              <div className="row between"><b>{a.title}</b><span className="muted small num">{new Date(a.at).toLocaleString()}</span></div>
              {a.body && <div className="small pre">{a.body}</div>}
              {Object.keys(a.delivered).length > 0 && <div className="small muted">{Object.entries(a.delivered).map(([k, v]) => `${k}: ${v}`).join(" · ")}</div>}
            </li>
          ))}
        </ul>
      )}
      {tab === "settings" && s && (
        <div className="alert-settings">
          <p className="small muted">The in-app feed always records everything. These channels get alerts at or above:</p>
          <select value={s.min_severity} onChange={(e) => save({ min_severity: e.target.value })} aria-label="Minimum severity">
            <option value="info">Everything (setups and fills too)</option>
            <option value="warn">Warnings and critical (stops, failed fetches, kill switch)</option>
            <option value="critical">Critical only (reconciliation breaks, exits not sent)</option>
          </select>

          <fieldset>
            <legend><label><input type="checkbox" checked={s.desktop} disabled={!s.desktop_supported} onChange={(e) => save({ desktop: e.target.checked })} /> Desktop notifications</label></legend>
            <p className="small muted">{s.desktop_supported ? "macOS Notification Center." : "Only available on macOS."}</p>
            <button className="btn small" disabled={!s.desktop_supported} onClick={() => test("desktop")}>Send test</button>
          </fieldset>

          <fieldset>
            <legend><label><input type="checkbox" checked={s.slack} onChange={(e) => save({ slack: e.target.checked })} /> Slack</label></legend>
            <p className="small muted">{s.has_slack_webhook ? "Webhook saved in keychain." : "Create an Incoming Webhook in Slack and paste its URL."}</p>
            <div className="row"><input className="grow" type="password" value={slackUrl} onChange={(e) => setSlackUrl(e.target.value)} placeholder="https://hooks.slack.com/services/…" />
              <button className="btn small" disabled={!slackUrl} onClick={() => saveSecret({ slack_webhook: slackUrl })}>Save</button></div>
            <button className="btn small" disabled={!s.has_slack_webhook} onClick={() => test("slack")}>Send test</button>
          </fieldset>

          <fieldset>
            <legend><label><input type="checkbox" checked={s.email} onChange={(e) => save({ email: e.target.checked })} /> Email</label></legend>
            <div className="grid2">
              <label>SMTP host<input value={s.smtp_host} onChange={(e) => setS({ ...s, smtp_host: e.target.value })} onBlur={() => save({})} placeholder="smtp.gmail.com" /></label>
              <label>Port<input type="number" value={s.smtp_port} onChange={(e) => setS({ ...s, smtp_port: Number(e.target.value) })} onBlur={() => save({})} /></label>
              <label>Username<input value={s.smtp_user} onChange={(e) => setS({ ...s, smtp_user: e.target.value })} onBlur={() => save({})} /></label>
              <label>Send to<input value={s.email_to} onChange={(e) => setS({ ...s, email_to: e.target.value })} onBlur={() => save({})} /></label>
            </div>
            <div className="row"><input className="grow" type="password" value={smtpPw} onChange={(e) => setSmtpPw(e.target.value)}
              placeholder={s.has_smtp_password ? "Password saved in keychain" : "SMTP or app password"} />
              <button className="btn small" disabled={!smtpPw} onClick={() => saveSecret({ smtp_password: smtpPw })}>Save</button></div>
            <button className="btn small" disabled={!s.has_smtp_password} onClick={() => test("email")}>Send test</button>
          </fieldset>
        </div>
      )}
    </div>
  );
}
