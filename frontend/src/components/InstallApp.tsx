import { useEffect, useState } from "react";
import { MonitorDown } from "lucide-react";

type PromptEvent = Event & { prompt: () => Promise<void>; userChoice: Promise<{ outcome: string }> };

/** "Install app" appears when the browser offers it (Chrome, Edge, Android). Safari users use Share → Add to Home Screen / Add to Dock. */
export function InstallApp({ notify }: { notify: (s: string) => void }) {
  const [evt, setEvt] = useState<PromptEvent | null>(null);
  useEffect(() => {
    const on = (e: Event) => { e.preventDefault(); setEvt(e as PromptEvent); };
    const done = () => { setEvt(null); notify("Tickmark is installed. Open it from your dock, home screen or app list."); };
    window.addEventListener("beforeinstallprompt", on); window.addEventListener("appinstalled", done);
    return () => { window.removeEventListener("beforeinstallprompt", on); window.removeEventListener("appinstalled", done); };
  }, [notify]);
  if (!evt) return null;
  return (
    <button className="iconbtn" title="Install Tickmark as an app" onClick={async () => { await evt.prompt(); await evt.userChoice; setEvt(null); }}>
      <MonitorDown size={16} aria-hidden /><span className="sr">Install app</span>
    </button>
  );
}
