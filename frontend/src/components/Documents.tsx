import { useEffect, useState } from "react";
import { api, ApiError } from "../api";

interface Cite { doc: "A" | "B"; start: number; end: number; quote: string }
interface Claim { type: "reported" | "calculated" | "interpretation" | "gap"; metric: string; text: string; cites: Cite[]; material?: boolean; calc?: any }

const TYPE_LABEL = { reported: "Reported fact", calculated: "Calculated", interpretation: "Interpretation", gap: "Not explained in source" };

function Highlighted({ text, hl }: { text: string; hl: Cite | null }) {
  if (!hl) return <pre className="doc">{text}</pre>;
  return (
    <pre className="doc">
      {text.slice(0, hl.start)}<mark id="hl">{text.slice(hl.start, hl.end)}</mark>{text.slice(hl.end)}
    </pre>
  );
}

export function Documents({ symbol }: { symbol: string }) {
  const [a, setA] = useState("");
  const [b, setB] = useState("");
  const [label, setLabel] = useState("");
  const [q, setQ] = useState("What changed, and does it affect my holding period?");
  const [res, setRes] = useState<any>(null);
  const [hl, setHl] = useState<Cite | null>(null);
  const [err, setErr] = useState<string | null>(null);
  const [editing, setEditing] = useState(true);

  useEffect(() => { api.get<any>("/api/documents/samples").then((s) => { setA(s.a); setB(s.b); setLabel(s.label); }); }, []);
  useEffect(() => { document.getElementById("hl")?.scrollIntoView({ block: "center", behavior: "smooth" }); }, [hl]);

  async function run() {
    setErr(null); setHl(null);
    try {
      setRes(await api.post("/api/documents/compare", { doc_a: a, doc_b: b, question: q, symbol }));
      setEditing(false);
    } catch (e) { setErr((e as ApiError).message); }
  }

  const byMetric: Record<string, Claim[]> = {};
  res?.claims.forEach((c: Claim) => { (byMetric[c.metric] ??= []).push(c); });

  return (
    <div className="docs">
      <p className="small muted">{label}</p>
      <div className="docs-grid">
        {(["A", "B"] as const).map((k) => {
          const text = k === "A" ? a : b;
          return (
            <div key={k}>
              <div className="row between"><b>Document {k} {k === "A" ? "(earlier)" : "(later)"}</b>
                {!editing && <button className="btn small ghost" onClick={() => setEditing(true)}>Edit text</button>}</div>
              {editing
                ? <textarea rows={9} value={text} onChange={(e) => (k === "A" ? setA : setB)(e.target.value)} aria-label={`Document ${k}`} />
                : <Highlighted text={text} hl={hl?.doc === k ? hl : null} />}
            </div>
          );
        })}
      </div>
      <div className="row">
        <input className="grow" value={q} onChange={(e) => setQ(e.target.value)} aria-label="Question" />
        <button className="btn primary" onClick={run}>Analyze against {symbol}'s holding window</button>
      </div>
      {err && <p className="err">{err}</p>}
      {res && (
        <div className="doc-results">
          <div className={`note ${res.holding_period.events_in_window.length ? "warn" : ""}`}>
            <b>Holding period:</b> {res.holding_period.verdict}
            {res.holding_period.events_in_window.map((e: any, i: number) => (
              <button key={i} className="cite" onClick={() => setHl(e.cite)}>{e.date} — doc {e.cite.doc}</button>
            ))}
          </div>
          <p className="small muted">{res.material_changes} material change(s) · {res.engine}. Click a citation to highlight the exact passage.</p>
          {Object.entries(byMetric).map(([m, cs]) => (
            <div key={m} className="metric">
              <h4>{m}</h4>
              {cs.map((c, i) => (
                <div key={i} className={`claim t-${c.type}${c.material ? " material" : ""}`}>
                  <span className="ctype">{TYPE_LABEL[c.type]}</span>
                  <span>{c.text}{c.calc && <small className="muted"> [{c.calc.formula}]</small>}</span>
                  <span className="cites">{c.cites.map((ct, k) => <button key={k} className="cite" onClick={() => setHl(ct)} title={ct.quote}>doc {ct.doc} ¶{ct.start}</button>)}</span>
                </div>
              ))}
            </div>
          ))}
          <p className="small muted">{res.unparsed_note}</p>
        </div>
      )}
    </div>
  );
}
