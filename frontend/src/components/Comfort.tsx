import { useEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Brain, Music2, MousePointer2, Pause, Timer } from "lucide-react";

/** Per-viewer preferences (browser storage): focus mode, custom cursor, music volume. */
function pref<T>(key: string, fallback: T): T {
  try { const v = localStorage.getItem("tickmark." + key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; }
}
function save(key: string, v: unknown) { try { localStorage.setItem("tickmark." + key, JSON.stringify(v)); } catch { /* private mode */ } }

export function useComfort() {
  const [focus, setFocus] = useState<boolean>(() => pref("focus", false));
  const [cursor, setCursor] = useState<boolean>(() => pref("cursor", true));
  const [theme, setTheme] = useState<"light" | "dark">(() => pref("theme", "light"));
  useEffect(() => {
    document.documentElement.dataset.theme = theme; save("theme", theme);
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", theme === "dark" ? "#0f1214" : "#e4e2dd");
  }, [theme]);
  useEffect(() => { document.body.classList.toggle("focus-mode", focus); save("focus", focus); }, [focus]);
  useEffect(() => { document.body.classList.toggle("tm-cursor", cursor); save("cursor", cursor); }, [cursor]);
  return { focus, setFocus, cursor, setCursor, theme, setTheme };
}

/** Calm generated background sound (no audio files): a soft chord pad over filtered brown noise, via Web Audio. */
class Ambient {
  ctx: AudioContext | null = null; gain: GainNode | null = null; nodes: AudioScheduledSourceNode[] = [];
  start(volume: number) {
    const ctx = new AudioContext(); this.ctx = ctx;
    const master = ctx.createGain(); master.gain.value = 0; master.connect(ctx.destination); this.gain = master;
    master.gain.linearRampToValueAtTime(volume, ctx.currentTime + 2.5);
    // brown noise -> low-pass: steady "rain" bed that masks distractions
    const buf = ctx.createBuffer(1, ctx.sampleRate * 4, ctx.sampleRate); const d = buf.getChannelData(0); let last = 0;
    for (let i = 0; i < d.length; i++) { last = (last + 0.02 * (Math.random() * 2 - 1)) / 1.02; d[i] = last * 3.2; }
    const noise = ctx.createBufferSource(); noise.buffer = buf; noise.loop = true;
    const lp = ctx.createBiquadFilter(); lp.type = "lowpass"; lp.frequency.value = 520;
    const ng = ctx.createGain(); ng.gain.value = 0.35; noise.connect(lp).connect(ng).connect(master); noise.start(); this.nodes.push(noise);
    // slow-breathing pad: A minor add9, each voice with its own gentle tremolo
    [110, 164.81, 220, 246.94, 329.63].forEach((f, i) => {
      const o = ctx.createOscillator(); o.type = i % 2 ? "sine" : "triangle"; o.frequency.value = f; o.detune.value = (i - 2) * 4;
      const g = ctx.createGain(); g.gain.value = 0.045;
      const lfo = ctx.createOscillator(); lfo.frequency.value = 0.05 + i * 0.017;
      const lg = ctx.createGain(); lg.gain.value = 0.03; lfo.connect(lg).connect(g.gain);
      o.connect(g).connect(master); o.start(); lfo.start(); this.nodes.push(o, lfo);
    });
  }
  volume(v: number) { if (this.ctx && this.gain) this.gain.gain.linearRampToValueAtTime(v, this.ctx.currentTime + 0.3); }
  stop() {
    const ctx = this.ctx; if (!ctx || !this.gain) return;
    this.gain.gain.linearRampToValueAtTime(0, ctx.currentTime + 1);
    setTimeout(() => { this.nodes.forEach((n) => { try { n.stop(); } catch { /* already stopped */ } }); ctx.close(); }, 1100);
    this.ctx = null; this.nodes = [];
  }
}

export function ComfortMenu({ focus, setFocus, cursor, setCursor, theme, setTheme }: ReturnType<typeof useComfort>) {
  const [open, setOpen] = useState(false);
  const [playing, setPlaying] = useState(false);
  const [vol, setVol] = useState<number>(() => pref("volume", 0.25));
  const amb = useRef<Ambient | null>(null);
  const box = useRef<HTMLDivElement>(null);
  useEffect(() => () => amb.current?.stop(), []);
  useEffect(() => {
    if (!open) return;
    const away = (e: MouseEvent) => { if (box.current && !box.current.contains(e.target as Node)) setOpen(false); };
    const esc = (e: KeyboardEvent) => { if (e.key === "Escape") setOpen(false); };
    document.addEventListener("mousedown", away); document.addEventListener("keydown", esc);
    return () => { document.removeEventListener("mousedown", away); document.removeEventListener("keydown", esc); };
  }, [open]);
  function toggleMusic() {
    if (playing) { amb.current?.stop(); setPlaying(false); return; }
    amb.current = new Ambient(); amb.current.start(vol); setPlaying(true);
  }
  return (
    <div className="comfort" ref={box}>
      <button className={`iconbtn ${focus || playing ? "lit" : ""}`} onClick={() => setOpen(!open)} aria-expanded={open} title="Focus mode, music, cursor">
        <Brain size={16} aria-hidden /><span className="sr">Focus and comfort</span>
      </button>
      <AnimatePresence>
        {open && (
          <motion.div className="comfort-pop card" initial={{ opacity: 0, y: -6, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.16, ease: [0.2, 0.8, 0.2, 1] }}>
            <label className="opt"><input type="checkbox" checked={focus} onChange={(e) => setFocus(e.target.checked)} />
              <span><b>Focus mode (ADHD-friendly)</b><small>Bigger text, fewer panels, one next step at a time, a 25-minute timer, no extra motion.</small></span></label>
            <div className="opt">
              <button className="btn small" onClick={toggleMusic}>{playing ? <Pause size={13} aria-hidden /> : <Music2 size={13} aria-hidden />} {playing ? "Pause music" : "Play focus music"}</button>
              <input type="range" min={0} max={0.6} step={0.02} value={vol} aria-label="Music volume"
                onChange={(e) => { const v = Number(e.target.value); setVol(v); save("volume", v); amb.current?.volume(v); }} />
            </div>
            <small className="muted">Generated in your browser: soft rain and a slow pad. Nothing is downloaded.</small>
            <label className="opt"><input type="checkbox" checked={theme === "dark"} onChange={(e) => setTheme(e.target.checked ? "dark" : "light")} />
              <span><b>Dark theme</b><small>Ink background for night use. Light warm stone is the default.</small></span></label>
            <label className="opt"><input type="checkbox" checked={cursor} onChange={(e) => setCursor(e.target.checked)} />
              <span><b><MousePointer2 size={13} aria-hidden /> Tickmark cursor</b><small>Accent-colored pointer, crosshair over charts.</small></span></label>
          </motion.div>
        )}
      </AnimatePresence>
    </div>
  );
}

/** Focus mode's single next step plus a 25-minute focus timer. */
export function FocusStrip({ step, onGo }: { step: { text: string; action?: string } ; onGo?: () => void }) {
  const [left, setLeft] = useState<number | null>(null);
  useEffect(() => {
    if (left == null) return;
    if (left <= 0) { setLeft(null); try { new Notification("Tickmark", { body: "Focus block done. Take a 5-minute break." }); } catch { /* not allowed */ } return; }
    const t = setTimeout(() => setLeft(left - 1), 1000); return () => clearTimeout(t);
  }, [left]);
  const mm = left == null ? "" : `${Math.floor(left / 60)}:${String(left % 60).padStart(2, "0")}`;
  return (
    <motion.div className="focus-strip" initial={{ opacity: 0, y: -8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }}>
      <span className="focus-k">Next step</span><span className="focus-v">{step.text}</span>
      {step.action && onGo && <button className="btn small primary" onClick={onGo}>{step.action}</button>}
      <span className="spacer" />
      <button className="btn small ghost" onClick={() => setLeft(left == null ? 25 * 60 : null)}><Timer size={13} aria-hidden /> {left == null ? "Start 25-min focus" : mm}</button>
    </motion.div>
  );
}
