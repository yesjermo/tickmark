"""Explanations generated FROM computed results, never the other way round.

Python computes every number and verdict (rules.evaluate_at). This module turns
that result into words for two audiences. The verdict and numbers are passed
through untouched; only the wording changes between Beginner and Analyst.

Optional: if SWINGDESK_LLM=claude and Anthropic credentials are available, the
facts JSON is sent to Claude for a friendlier narrative. The reply is rejected
(and the deterministic text used instead) if it contains any number that is not
present in the facts, so the model cannot invent figures.
"""
from __future__ import annotations

import json
import os
import re

STATUS_WORDS = {
    "QUALIFIES": ("Meets your setup", "All rules pass."),
    "QUALIFIES_WITH_WARNING": ("Meets your setup, with a warning", "All hard rules pass; earnings fall inside the holding window."),
    "NEAR_MISS": ("Near miss", "Exactly one hard rule failed."),
    "NEEDS_REVIEW": ("Needs review: information missing", "No hard rule failed, but at least one input is unknown."),
    "NO_SETUP": ("No setup", "Two or more hard rules failed."),
}


def _vals(f: dict, ev: dict) -> dict:
    p = ev["params"]
    v = lambda k: (f.get(k) or {}).get("value")  # noqa: E731
    ratio = v("volume_ratio")
    return {"ratio_x": f"{ratio:.1f}" if ratio else "?", "ratio_pct": round(ratio * 100) if ratio else "?",
            "rsi": f"{v('rsi'):.0f}" if v("rsi") is not None else "?", "gap": f"{v('gap'):.1f}" if v("gap") is not None else "?",
            "report": v("report") or "?", **{k: p.get(k) for k in ("breakout_lookback", "trend_sma", "rsi_max", "fast_ema",
                                                                      "slow_sma", "gap_min_pct", "volume_ratio_min", "slope_lookback")}}


# Beginner wording for the strategy library's rules. Numbers come from the computed facts only.
PLAIN = {
    "new_high": {"pass": "The price closed above its highest point of the last {breakout_lookback} trading days: a breakout.",
                 "fail": "The price did not close above its highest point of the last {breakout_lookback} trading days.",
                 "unknown": "We can't tell whether it broke out (price history missing)."},
    "heavier_volume": {"pass": "Lots of shares changed hands (about {ratio_x} times a normal day), so many buyers were behind the move.",
                       "fail": "Volume was only about {ratio_x} times normal; your rule wants at least {volume_ratio_min}x, so the move lacked conviction.",
                       "unknown": "We can't compare today's volume with normal (volume history missing)."},
    "strong_close": {"pass": "It finished near the top of the day's range: buyers held on into the close.",
                     "fail": "It gave back much of the day's gain before the close, which weakens a breakout.",
                     "unknown": "We can't tell where it closed within the day's range."},
    "uptrend": {"pass": "The long-term trend is still up: the price is above its {trend_sma}-day average.",
                "fail": "The price is below its {trend_sma}-day average, so the long-term trend isn't up. This setup only buys dips in uptrends.",
                "unknown": "Not enough history to judge the long-term trend."},
    "oversold": {"pass": "It has dropped sharply in the last few days (RSI {rsi}, below {rsi_max}): a short-term washout that often bounces.",
                 "fail": "It hasn't fallen far enough to count as a washout (RSI {rsi}; your rule wants below {rsi_max}).",
                 "unknown": "We can't measure how oversold it is (data missing)."},
    "cross_up": {"pass": "Today the shorter {fast_ema}-day average moved above the longer {slow_sma}-day average: a possible new uptrend.",
                 "fail": "The {fast_ema}-day average did not cross above the {slow_sma}-day average today.",
                 "unknown": "Not enough history to compare the averages."},
    "slow_rising": {"pass": "The {slow_sma}-day average is pointing up compared with {slope_lookback} days ago.",
                    "fail": "The {slow_sma}-day average is flat or falling, so the bigger trend hasn't turned up yet.",
                    "unknown": "Not enough history to tell which way the average is moving."},
    "recent_report": {"pass": "The company just reported earnings ({report}), which is the news behind today's move.",
                      "fail": "There was no earnings report just before today, so this isn't a post-earnings move.",
                      "unknown": "We can't tell whether earnings were just reported."},
    "gap_up": {"pass": "It opened {gap}% above yesterday's close, a gap up on the news.",
               "fail": "It opened only {gap}% above yesterday's close; your rule wants at least {gap_min_pct}%.",
               "unknown": "We can't measure the opening gap (data missing)."},
    "held_gap": {"pass": "It closed at or above where it opened, so buyers kept the gap instead of selling it.",
                 "fail": "It closed below where it opened: sellers faded the gap.",
                 "unknown": "We can't tell whether the gap held."},
}


def _beginner_line(r: dict, ev: dict) -> str:
    f = ev["facts"]
    rid, res = r["id"], r["result"]
    if rid == "data":
        return "There is enough price history to judge this setup." if res == "pass" else "Some price history is missing, so parts of this check can't be judged."
    if rid == "pullback_touch":
        return {"pass": "During the day, the price dipped down to its recent average price.",
                "fail": "The price stayed above its recent average all day, so it never really pulled back to it.",
                "unknown": "We can't tell whether the price reached its recent average (data missing)."}[res]
    if rid == "close_above":
        return {"pass": "By the end of the day it finished at or above that average, so buyers stepped back in.",
                "fail": "It finished the day below that average, which is weaker than this setup wants.",
                "unknown": "We can't tell where it finished relative to the average."}[res]
    if rid == "lighter_volume":
        ratio = f["volume_ratio"]["value"]
        if res == "unknown":
            return "We can't compare today's trading activity with normal (volume history missing)."
        pct = round(ratio * 100)
        return (f"Fewer shares traded than usual (about {pct}% of a normal day), which suggests the dip was quiet, not a rush for the exits."
                if res == "pass" else
                f"Trading was not quiet enough: about {pct}% of a normal day, and your rule asks for less than {round(ev['params']['volume_ratio_max'] * 100)}%.")
    if rid in PLAIN:
        txt = PLAIN[rid].get(res) or PLAIN[rid].get("unknown")
        return txt.format(**_vals(f, ev)) if txt else r["name"]
    if rid == "earnings":
        return {"pass": "The company is not scheduled to report earnings while you'd be holding.",
                "warn": "Heads up: the company reports earnings while you'd be holding. Earnings can move a stock sharply either way.",
                "fail": "The company reports earnings while you'd be holding, and your rules say to skip those.",
                "unknown": "We don't know when the company reports next, so we can't rule out an earnings surprise."}[res]
    return r["name"]


def explain(ev: dict, params: dict, level: str, allow_llm: bool = True) -> dict:
    ev = {**ev, "params": params}
    title, summary = STATUS_WORDS[ev["status"]]
    f = ev["facts"]
    bar = f["bar"]["value"]
    if level == "beginner":
        lines = [{"text": _beginner_line(r, ev), "rule": r["id"], "result": r["result"], "facts": r["facts"]} for r in ev["rules"]]
        if ev["status"] == "NEAR_MISS":
            miss = next(r for r in ev["rules"] if r["id"] in ev["failed"])
            lines.append({"text": f"It dropped off your list for one reason: {miss['name'].lower()}.", "rule": miss["id"],
                          "result": "fail", "facts": miss["facts"]})
    else:
        lines = []
        for r in ev["rules"]:
            txt = f"{r['name']}: {r['observed']} vs {r['threshold']} -> {r['result'].upper()}"
            if r.get("margin"):
                txt += f" ({r['margin']})"
            lines.append({"text": txt, "rule": r["id"], "result": r["result"], "facts": r["facts"]})
        lines.append({"text": (f"Candle {bar['date']}: O {bar['open']} H {bar['high']} L {bar['low']} C {bar['close']}, "
                               f"V {bar['volume']:,.0f}. Data timestamp {bar['date']} 16:00 ET close (completed bar)."),
                      "rule": "data", "result": "info", "facts": ["bar"]})
    out = {"level": level, "status": ev["status"], "title": title, "summary": summary, "lines": lines,
           "narrator": "deterministic templates", "llm_note": None}
    if allow_llm and os.environ.get("SWINGDESK_LLM") == "claude":     # guests never spend the server's AI key
        out = _try_claude(out, ev, level)
    return out


def _numbers(s: str) -> set[str]:
    return {n.replace(",", "").rstrip(".").lstrip("0") or "0" for n in re.findall(r"\d[\d,]*\.?\d*", s)}


def _try_claude(out: dict, ev: dict, level: str) -> dict:
    try:
        import anthropic
        client = anthropic.Anthropic()
        facts = {"status": ev["status"], "rules": ev["rules"], "facts": {k: v for k, v in ev["facts"].items() if k != "avg_volume"},
                 "params": ev["params"]}
        allowed = _numbers(json.dumps(facts)) | {str(i) for i in range(0, 101)}
        audience = ("a beginner who has never traded; no jargon, two to four short sentences"
                    if level == "beginner" else "an experienced analyst; terse, cite thresholds and values")
        resp = client.beta.messages.create(
            model="claude-opus-5-5",
            max_tokens=2000,
            output_config={"effort": "low"},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            system=("You explain stock-screen results that were computed by code. Use ONLY the numbers in the JSON. "
                    "Do not change the verdict, add predictions, or give advice. Do not use numbers that are not in the JSON."),
            messages=[{"role": "user", "content": f"Explain this result for {audience}.\n\n{json.dumps(facts)}"}],
        )
        if resp.stop_reason == "refusal":
            out["llm_note"] = "Claude declined; showing deterministic text."
            return out
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        stray = _numbers(text) - allowed
        if stray:
            out["llm_note"] = f"Claude reply rejected: it used numbers not in the computed facts ({', '.join(sorted(stray)[:5])})."
            return out
        out["narrative"] = text
        out["narrator"] = "claude-opus-5-5 (numbers checked against computed facts)"
    except Exception as exc:  # no credentials, network, etc. -> deterministic text stands
        out["llm_note"] = f"Claude unavailable ({type(exc).__name__}); showing deterministic text."
    return out
