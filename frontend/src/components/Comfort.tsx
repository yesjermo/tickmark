import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { AnimatePresence, motion } from "motion/react";
import { Brain, Music2, MousePointer2, Pause, Timer } from "lucide-react";

/** Per-viewer preferences (browser storage): focus mode, custom cursor, music volume. */
function pref<T>(key: string, fallback: T): T {
  try { const v = localStorage.getItem("tickmark." + key); return v == null ? fallback : JSON.parse(v); } catch { return fallback; }
}
function save(key: string, v: unknown) { try { localStorage.setItem("tickmark." + key, JSON.stringify(v)); } catch { /* private mode */ } }

export type Mode = "system" | "light" | "dark";
export type Motion = "full" | "subtle" | "off";
export const PALETTES: { id: string; name: string; swatch: [string, string, string] }[] = [
  { id: "stone", name: "Tickmark Stone", swatch: ["#e4e2dd", "#101417", "#5e844a"] },
  { id: "github", name: "GitHub", swatch: ["#f6f8fa", "#0969da", "#1a7f37"] },
  { id: "one", name: "One Dark / One Light", swatch: ["#282c34", "#61afef", "#98c379"] },
  { id: "dracula", name: "Dracula / Alucard", swatch: ["#282a36", "#bd93f9", "#50fa7b"] },
  { id: "nord", name: "Nord", swatch: ["#2e3440", "#88c0d0", "#a3be8c"] },
  { id: "solarized", name: "Solarized", swatch: ["#002b36", "#268bd2", "#859900"] },
  { id: "monokai", name: "Monokai", swatch: ["#272822", "#66d9ef", "#a6e22e"] },
];

const systemDark = () => { try { return window.matchMedia("(prefers-color-scheme: dark)").matches; } catch { return false; } };

export function useComfort() {
  const [focus, setFocus] = useState<boolean>(() => pref("focus", false));
  const [cursor, setCursor] = useState<boolean>(() => pref("cursor", true));
  const [mode, setMode] = useState<Mode>(() => pref("mode", "system"));
  const [palette, setPalette] = useState<string>(() => pref("palette", "stone"));
  const [motionLevel, setMotionLevel] = useState<Motion>(() => pref("motion", "full"));
  const [smooth, setSmooth] = useState<boolean>(() => pref("smoothScroll", true));
  const [reveal, setReveal] = useState<boolean>(() => pref("reveal", true));
  const [sysDark, setSysDark] = useState(systemDark);
  useEffect(() => {   // "System" follows the computer's setting live, e.g. when macOS switches at sunset
    let mq: MediaQueryList | null = null;
    try { mq = window.matchMedia("(prefers-color-scheme: dark)"); } catch { return; }
    const on = () => setSysDark(mq!.matches);
    mq.addEventListener("change", on);
    return () => mq!.removeEventListener("change", on);
  }, []);
  const theme: "light" | "dark" = mode === "system" ? (sysDark ? "dark" : "light") : mode;
  useLayoutEffect(() => {     // before paint and before charts read their colors
    const root = document.documentElement;
    root.dataset.theme = theme; root.dataset.palette = palette;
    save("mode", mode); save("palette", palette);
    const bg = getComputedStyle(root).getPropertyValue("--bg").trim();
    document.querySelector('meta[name="theme-color"]')?.setAttribute("content", bg || (theme === "dark" ? "#0f1214" : "#e4e2dd"));
  }, [theme, palette, mode]);
  useEffect(() => { document.documentElement.dataset.motion = motionLevel; save("motion", motionLevel); }, [motionLevel]);
  useEffect(() => { document.documentElement.dataset.reveal = reveal ? "on" : "off"; save("reveal", reveal); }, [reveal]);
  useEffect(() => { save("smoothScroll", smooth); }, [smooth]);
  useEffect(() => { document.body.classList.toggle("focus-mode", focus); save("focus", focus); }, [focus]);
  useEffect(() => { document.body.classList.toggle("tm-cursor", cursor); save("cursor", cursor); }, [cursor]);
  const setTheme = (t: "light" | "dark") => setMode(t);   // the rail's moon/sun button picks a fixed mode
  return { focus, setFocus, cursor, setCursor, theme, setTheme, mode, setMode, palette, setPalette,
    motionLevel, setMotionLevel, smooth: smooth && motionLevel !== "off" && !focus, setSmooth, smoothPref: smooth, reveal, setReveal };
}

/** Inertia scrolling (Lenis) on one scroll container, off when motion is off, in focus mode, or on touch screens (native is better there). */
export function useSmoothScroll(enabled: boolean) {
  const [el, setEl] = useState<HTMLElement | null>(null);
  useEffect(() => {
    if (!el || !enabled || window.matchMedia("(pointer: coarse)").matches) return;
    let alive = true, raf = 0;
    let lenis: { raf: (t: number) => void; destroy: () => void } | null = null;
    import("lenis").then(({ default: Lenis }) => {
      if (!alive) return;
      lenis = new Lenis({ wrapper: el, content: el, lerp: 0.14, smoothWheel: true, wheelMultiplier: 0.95,
        // nested scrollers, form fields and charts keep their own wheel handling
        prevent: (node: HTMLElement) => !!node.closest?.("textarea, select, [data-lenis-prevent], .tv-chart, .eq-chart, .modal-scrim") });
      const loop = (t: number) => { lenis?.raf(t); raf = requestAnimationFrame(loop); };
      raf = requestAnimationFrame(loop);
    }).catch(() => { /* native scrolling */ });
    return () => { alive = false; cancelAnimationFrame(raf); lenis?.destroy(); };
  }, [el, enabled]);
  return setEl;
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

export function ComfortMenu({ focus, setFocus, cursor, setCursor, mode, setMode, palette, setPalette, motionLevel, setMotionLevel, smoothPref, setSmooth, reveal, setReveal }: ReturnType<typeof useComfort>) {
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
      <button className={`iconbtn ${focus || playing ? "lit" : ""}`} onClick={() => setOpen(!open)} aria-expanded={open} title="Appearance, motion, focus mode and music">
        <Brain size={16} aria-hidden /><span className="sr">Display and comfort settings</span>
      </button>
      <AnimatePresence>
        {open && (
          <motion.div className="comfort-pop card" initial={{ opacity: 0, y: -6, scale: 0.98 }} animate={{ opacity: 1, y: 0, scale: 1 }}
            exit={{ opacity: 0, y: -4 }} transition={{ duration: 0.16, ease: [0.2, 0.8, 0.2, 1] }} data-lenis-prevent>
            <h4 className="pop-h">Appearance</h4>
            <div className="seg full" role="group" aria-label="Light or dark">
              {(["system", "light", "dark"] as Mode[]).map((m) => (
                <button key={m} className={mode === m ? "on" : ""} aria-pressed={mode === m} onClick={() => setMode(m)}>
                  {m === "system" ? "System" : m === "light" ? "Light" : "Dark"}</button>
              ))}
            </div>
            <div className="palettes" role="radiogroup" aria-label="Color theme">
              {PALETTES.map((p) => (
                <button key={p.id} role="radio" aria-checked={palette === p.id} className={`pal ${palette === p.id ? "on" : ""}`} onClick={() => setPalette(p.id)}>
                  <span className="pal-sw" aria-hidden>{p.swatch.map((c) => <i key={c} style={{ background: c }} />)}</span>{p.name}
                </button>
              ))}
            </div>
            <small className="muted">System follows your computer's light or dark setting. IDE themes come in both versions.</small>
            <h4 className="pop-h">Motion and scrolling</h4>
            <div className="seg full" role="group" aria-label="Motion">
              {(["full", "subtle", "off"] as Motion[]).map((m) => (
                <button key={m} className={motionLevel === m ? "on" : ""} aria-pressed={motionLevel === m} onClick={() => setMotionLevel(m)}>
                  {m === "full" ? "Full" : m === "subtle" ? "Subtle" : "Off"}</button>
              ))}
            </div>
            <label className="opt"><input type="checkbox" checked={smoothPref} onChange={(e) => setSmooth(e.target.checked)} />
              <span><b>Smooth scrolling</b><small>Inertia on mouse wheels and trackpads. Phones keep their native scrolling.</small></span></label>
            <label className="opt"><input type="checkbox" checked={reveal} onChange={(e) => setReveal(e.target.checked)} />
              <span><b>Scroll effects</b><small>Cards ease in as they scroll into view.</small></span></label>
            <h4 className="pop-h">Focus</h4>
            <label className="opt"><input type="checkbox" checked={focus} onChange={(e) => setFocus(e.target.checked)} />
              <span><b>Focus mode (ADHD-friendly)</b><small>Bigger text, fewer panels, one next step at a time, a 25-minute timer, no extra motion.</small></span></label>
            <div className="opt">
              <button className="btn small" onClick={toggleMusic}>{playing ? <Pause size={13} aria-hidden /> : <Music2 size={13} aria-hidden />} {playing ? "Pause music" : "Play focus music"}</button>
              <input type="range" min={0} max={0.6} step={0.02} value={vol} aria-label="Music volume"
                onChange={(e) => { const v = Number(e.target.value); setVol(v); save("volume", v); amb.current?.volume(v); }} />
            </div>
            <small className="muted">Generated in your browser: soft rain and a slow pad. Nothing is downloaded.</small>
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
      <button className="btn small ghost" onClick={() => {
        if (left == null && "Notification" in window && Notification.permission === "default") Notification.requestPermission().catch(() => {});
        setLeft(left == null ? 25 * 60 : null);
      }}><Timer size={13} aria-hidden /> {left == null ? "Start 25-min focus" : mm}</button>
    </motion.div>
  );
}
