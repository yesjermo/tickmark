"""Compare two earnings releases and cite exact passages.

Deterministic extraction (no model needed): sentences are split with character
offsets, metrics are matched by keyword, values parsed from the sentence, and
changes computed in Python. Every claim carries a type:

  reported      - stated in a document (with the exact quoted passage + offsets)
  calculated    - computed here from two reported values (cites both)
  interpretation- a rule-based reading, labelled as such, never presented as fact
  gap           - the source does not explain a material change

If the later document gives no reason for a material change, the output says so.
"""
from __future__ import annotations

import re
from datetime import date

SAMPLE_A = """FICTIONAL SAMPLE - Vantage Compute Corp. (ticker: VNTG) reports second quarter fiscal 2027 results.
SANTA CLARA, Calif., August 26, 2026. Vantage Compute Corp. today reported revenue of $8.10 billion for the second quarter ended July 26, 2026, up 9% from the prior quarter.
Data center revenue was $6.20 billion, driven by shipments of the V40 accelerator to cloud customers.
GAAP gross margin was 71.2%.
Operating expenses were $1.45 billion.
Diluted earnings per share were $1.12.
Outlook: For the third quarter of fiscal 2027, revenue is expected to be $9.60 billion, plus or minus 2%.
The company expects to report third quarter results on November 18, 2026."""

SAMPLE_B = """FICTIONAL SAMPLE - Vantage Compute Corp. (ticker: VNTG) reports third quarter fiscal 2027 results.
SANTA CLARA, Calif., November 18, 2026. Vantage Compute Corp. today reported revenue of $9.02 billion for the third quarter ended October 25, 2026, up 11% from the prior quarter.
Data center revenue was $7.30 billion, driven by continued demand for the V40 accelerator and initial V50 shipments.
GAAP gross margin was 68.9%.
Operating expenses were $1.61 billion, reflecting higher headcount and new product introduction costs.
Diluted earnings per share were $1.19.
Outlook: For the fourth quarter of fiscal 2027, revenue is expected to be $9.40 billion, plus or minus 2%.
The company will host an investor day on November 30, 2026, where management will discuss its product roadmap."""

METRICS = [
    ("data_center_revenue", "Data center revenue", r"data center revenue"),
    ("revenue", "Revenue", r"(?<!center )\brevenue of\b|\breported revenue\b"),
    ("gross_margin", "Gross margin", r"gross margin"),
    ("opex", "Operating expenses", r"operating expenses"),
    ("eps", "Diluted EPS", r"earnings per share"),
    ("guidance", "Next-quarter revenue outlook", r"outlook|expected to be|guidance"),
    ("net_income", "Net income", r"net income"),
    ("fcf", "Free cash flow", r"free cash flow"),
]
CAUSAL = re.compile(r"\b(driven by|due to|reflecting|as a result of|primarily from|because of|attributable to|led by)\b", re.I)
VALUE = re.compile(r"(\$\s?\d+(?:\.\d+)?\s?(?:billion|million|thousand)?|\d+(?:\.\d+)?\s?%)", re.I)
DATE = re.compile(r"(January|February|March|April|May|June|July|August|September|October|November|December)\s+(\d{1,2}),\s+(\d{4})")
MONTHS = {m: i for i, m in enumerate(["January", "February", "March", "April", "May", "June", "July", "August",
                                         "September", "October", "November", "December"], 1)}


def sentences(text: str) -> list[dict]:
    out = []
    for m in re.finditer(r"[^\n]+", text):
        line, base = m.group(0), m.start()
        for s in re.finditer(r".+?(?:(?<!\d)\.(?=\s|$)|$)", line):
            seg = s.group(0).strip()
            if seg:
                start = base + s.start() + (len(s.group(0)) - len(s.group(0).lstrip()))
                out.append({"text": seg, "start": start, "end": start + len(seg)})
    return out


def _parse_value(raw: str) -> tuple[float, str]:
    r = raw.lower().replace(" ", "")
    if r.endswith("%"):
        return float(r[:-1]), "%"
    num = float(re.sub(r"[^\d.]", "", r))
    if "billion" in r:
        num *= 1e9
    elif "million" in r:
        num *= 1e6
    elif "thousand" in r:
        num *= 1e3
    return num, "$"


def extract(text: str, doc: str) -> dict:
    found = {}
    for s in sentences(text):
        low = s["text"].lower()
        for key, label, pat in METRICS:
            if key in found or not re.search(pat, low):
                continue
            if key == "revenue" and "data center" in low:
                continue
            v = None
            kw = re.search(pat, low)
            for vm in VALUE.finditer(s["text"]):
                if vm.start() >= kw.start():
                    v = vm
                    break
            if not v:
                continue
            val, unit = _parse_value(v.group(0))
            cause = CAUSAL.search(s["text"])
            found[key] = {"metric": key, "label": label, "value": val, "unit": unit, "raw": v.group(0),
                          "cite": {"doc": doc, "start": s["start"], "end": s["end"], "quote": s["text"]},
                          "stated_reason": s["text"][cause.start():].rstrip(".") if cause else None}
            break
    return found


def find_dates(text: str, doc: str) -> list[dict]:
    out = []
    for s in sentences(text):
        for m in DATE.finditer(s["text"]):
            d = date(int(m.group(3)), MONTHS[m.group(1)], int(m.group(2)))
            out.append({"date": d.isoformat(), "cite": {"doc": doc, "start": s["start"], "end": s["end"], "quote": s["text"]}})
    return out


def _fmt(v, unit):
    if unit == "%":
        return f"{v:.1f}%"
    if abs(v) >= 1e9:
        return f"${v / 1e9:.2f}B"
    if abs(v) >= 1e6:
        return f"${v / 1e6:.0f}M"
    return f"${v:.2f}"


def compare(doc_a: str, doc_b: str, window: dict | None, question: str = "") -> dict:
    a, b = extract(doc_a, "A"), extract(doc_b, "B")
    claims = []
    for key, label, _ in METRICS:
        if key not in a and key not in b:
            continue
        if key in a and key in b:
            fa, fb = a[key], b[key]
            claims.append({"type": "reported", "metric": label, "text": f"Document A: {label} {_fmt(fa['value'], fa['unit'])}.", "cites": [fa["cite"]]})
            claims.append({"type": "reported", "metric": label, "text": f"Document B: {label} {_fmt(fb['value'], fb['unit'])}.", "cites": [fb["cite"]]})
            if fa["unit"] == "%":
                delta, txt = fb["value"] - fa["value"], f"{fb['value'] - fa['value']:+.1f} percentage points"
                material = abs(delta) >= 1.0
            else:
                delta = (fb["value"] / fa["value"] - 1) * 100
                txt = f"{delta:+.1f}% ({_fmt(fa['value'], '$')} -> {_fmt(fb['value'], '$')})"
                material = abs(delta) >= 5.0
            claims.append({"type": "calculated", "metric": label, "text": f"{label} changed {txt}.", "material": material,
                           "calc": {"a": fa["value"], "b": fb["value"], "unit": fa["unit"],
                                    "formula": "B - A (pts)" if fa["unit"] == "%" else "(B / A - 1) x 100"},
                           "cites": [fa["cite"], fb["cite"]]})
            if material:
                if fb["stated_reason"]:
                    claims.append({"type": "reported", "metric": label, "text": f"Company's stated reason: \"{fb['stated_reason']}\".", "cites": [fb["cite"]]})
                else:
                    claims.append({"type": "gap", "metric": label,
                                   "text": f"Document B gives no explanation for the change in {label}. Any reason would be speculation.",
                                   "cites": [fb["cite"]], "no_explanation": True})
            if key == "guidance" and fb["value"] < fa["value"]:
                claims.append({"type": "interpretation", "metric": label,
                               "text": "Rule-based reading: the newer outlook is below the prior one, a negative revision to forward expectations. This is an interpretation, not a reported fact.",
                               "cites": [fa["cite"], fb["cite"]]})
        else:
            f = a.get(key) or b.get(key)
            other = "B" if key in a else "A"
            claims.append({"type": "reported", "metric": label, "text": f"Document {f['cite']['doc']}: {label} {_fmt(f['value'], f['unit'])}. Not stated in document {other}, so no change can be calculated.",
                           "cites": [f["cite"]]})

    dates = find_dates(doc_a, "A") + find_dates(doc_b, "B")
    holding = {"window": window, "events_in_window": [], "verdict": None}
    if window:
        inside = [d for d in dates if window["entry_date"] <= d["date"] <= window["exit_date"]]
        holding["events_in_window"] = inside
        if inside:
            holding["verdict"] = (f"{len(inside)} dated event(s) in these documents fall inside your holding window "
                                  f"({window['entry_date']} to {window['exit_date']}). Scheduled company events can move the price; "
                                  "the documents do not say how.")
        else:
            holding["verdict"] = (f"Neither document mentions a date inside your holding window ({window['entry_date']} to {window['exit_date']}). "
                                  "The releases describe past results and guidance; they cannot tell you how the stock will trade during the hold.")
    material = [c for c in claims if c["type"] == "calculated" and c.get("material")]
    return {"question": question or "What changed, and does it affect my holding period?",
            "engine": "deterministic extractor (keyword + value parsing); no model involved",
            "claims": claims, "material_changes": len(material), "holding_period": holding, "dates_found": dates,
            "unparsed_note": "Only these metrics are recognised: " + ", ".join(m[1] for m in METRICS) + ". Anything else in the text is not analysed."}
