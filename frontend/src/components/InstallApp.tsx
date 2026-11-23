import { useEffect, useState } from "react";
import { MonitorDown } from "lucide-react";

type PromptEvent = Event & { prompt: () => Promise<void>; userChoice: Promise<{ outcome: string }> };

// The browser offers installation once per page load, possibly before the desk mounts (or while it remounts after sign-in):
// keep the offer at module level.
let offered: PromptEvent | null = null;
window.addEventListener("beforeinstallprompt", (e) => { e.preventDefault(); offered = e as PromptEvent; window.dispatchEvent(new Event("tickmark:installable")); });

/** "Install app" appears when the browser offers it (Chrome, Edge, Android). Safari users use Share → Add to Home Screen / Add to Dock. */
export function InstallApp({ notify }: { notify: (s: string) => void }) {
  const [evt, setEvt] = useState<PromptEvent | null>(offered);
  useEffect(() => {
    const on = () => setEvt(offered);
    const done = () => { setEvt(null); notify("Tickmark is installed. Open it from your dock, home screen or app list."); };
    window.addEventListener("tickmark:installable", on); window.addEventListener("appinstalled", done);
    return () => { window.removeEventListener("tickmark:installable", on); window.removeEventListener("appinstalled", done); };
  }, [notify]);
  if (!evt) return null;
  return (
    <button className="iconbtn" title="Install Tickmark as an app" onClick={async () => { await evt.prompt(); await evt.userChoice; offered = null; setEvt(null); }}>
      <MonitorDown size={16} aria-hidden /><span className="sr">Install app</span>
    </button>
  );
}
