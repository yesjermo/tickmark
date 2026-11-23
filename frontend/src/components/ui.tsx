import type { Result, Status } from "../api";

export const money = (n: number | null | undefined, d = 2) =>
  n == null ? "—" : n.toLocaleString("en-US", { style: "currency", currency: "USD", minimumFractionDigits: d, maximumFractionDigits: d });
export const num = (n: number | null | undefined, d = 2) => (n == null ? "—" : n.toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d }));
export const pct = (n: number | null | undefined, d = 1) => (n == null ? "—" : `${n > 0 ? "+" : ""}${n.toFixed(d)}%`);

export const STATUS_LABEL: Record<Status, string> = {
  QUALIFIES: "Qualifies",
  QUALIFIES_WITH_WARNING: "Qualifies · warning",
  NEAR_MISS: "Near miss",
  NEEDS_REVIEW: "Missing info",
  NO_SETUP: "No setup",
};

export function StatusChip({ s }: { s: Status }) {
  return <span className={`chip st-${s}`}>{STATUS_LABEL[s]}</span>;
}

const RESULT_GLYPH: Record<string, string> = { pass: "✓", fail: "✕", warn: "!", unknown: "?", info: "i" };
export function ResultBadge({ r }: { r: Result | "info" }) {
  return <span className={`res res-${r}`}>{RESULT_GLYPH[r]} {r === "info" ? "data" : r}</span>;
}

export function OrderStatus({ s }: { s: string }) {
  return <span className={`ostatus os-${s}`}>{s.replace("_", " ")}</span>;
}
