import { RefObject, useEffect, useRef, useState } from "react";

let probe: CanvasRenderingContext2D | null = null;

/** A theme token as a color lightweight-charts can parse. Tokens may be color-mix(...) (IDE themes), which the chart library
 *  can't read, so it goes through computed style (resolves var/color-mix) and a canvas round-trip (normalizes to #hex / rgba). */
export function resolveColor(name: string): string {
  const root = document.documentElement;
  const raw = getComputedStyle(root).getPropertyValue(name).trim();
  if (/^#[0-9a-f]{3,8}$/i.test(raw) || /^rgba?\(/i.test(raw)) return raw;
  const el = document.createElement("span");
  el.style.color = `var(${name})`;
  el.style.display = "none";
  root.appendChild(el);
  const computed = getComputedStyle(el).color;
  el.remove();
  // Browsers keep color-mix results as color(srgb ...), which lightweight-charts can't parse: paint one pixel and read it back.
  if (!probe) { const cv = document.createElement("canvas"); cv.width = cv.height = 1; probe = cv.getContext("2d", { willReadFrequently: true }); }
  if (!probe) return "#888888";
  probe.clearRect(0, 0, 1, 1);
  probe.fillStyle = "#888888";
  probe.fillStyle = computed;
  probe.fillRect(0, 0, 1, 1);
  const [r, g, b, a] = probe.getImageData(0, 0, 1, 1).data;
  return a >= 255 ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${(a / 255).toFixed(3)})`;
}

/** Changes whenever the light/dark mode or color theme changes, so charts can redraw in the new colors (and nothing else remounts). */
export function useThemeKey(): string {
  const read = () => `${document.documentElement.dataset.theme ?? "light"}:${document.documentElement.dataset.palette ?? "stone"}`;
  const [key, setKey] = useState(read);
  useEffect(() => {
    const mo = new MutationObserver(() => setKey(read()));
    mo.observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "data-palette"] });
    return () => mo.disconnect();
  }, []);
  return key;
}

/** Modal behavior: focus moves in, Tab stays inside, Escape closes, focus returns where it was. */
export function useDialog(ref: RefObject<HTMLElement>, onClose: () => void, enabled = true) {
  const close = useRef(onClose);
  close.current = onClose;           // latest callback without re-running the effect (which would steal focus while typing)
  useEffect(() => {
    if (!enabled) return;
    const before = document.activeElement as HTMLElement | null;
    const box = ref.current;
    const focusables = () => Array.from(box?.querySelectorAll<HTMLElement>(
      'input:not([disabled]):not([type=hidden]), select, textarea, button:not([disabled]), a[href], [tabindex]:not([tabindex="-1"])') ?? []);
    const first = focusables().find((el) => el.tagName === "INPUT") ?? focusables()[0];
    first?.focus({ preventScroll: true });
    const key = (e: KeyboardEvent) => {
      if (e.key === "Escape") { e.stopPropagation(); close.current(); return; }
      if (e.key !== "Tab") return;
      const f = focusables();
      if (!f.length) return;
      const a = f[0], z = f[f.length - 1];
      if (e.shiftKey && document.activeElement === a) { e.preventDefault(); z.focus(); }
      else if (!e.shiftKey && document.activeElement === z) { e.preventDefault(); a.focus(); }
    };
    document.addEventListener("keydown", key);
    return () => { document.removeEventListener("keydown", key); before?.focus?.({ preventScroll: true }); };
  }, [ref, enabled]);
}
