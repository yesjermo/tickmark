# Designing for expansion

The swing-trading demo is built from five reusable pieces. Each new workflow below is mostly new **inputs and rule definitions** on top of the same pieces. None of these are built yet.

| Component | Today (swing) | Reused as |
|---|---|---|
| **Data layer** (`bars`, `earnings`, `Series`) | Daily OHLCV + earnings calendar | Add `quotes`, `intraday_bars`, `filings`, `financials` tables with the same `source` + timestamp columns |
| **Calculation core** (`indicators.py`, `rules.evaluate_at`) | EMA, trailing averages, pass/fail/unknown | The same contract: a pure function over data up to time t that returns rules and facts with `inputs` |
| **Evidence + explanation** (`explain.py`, click-through facts) | Beginner/Analyst wording of computed facts | The same, for any metric. The LLM only ever sees facts and is number-guarded. |
| **Document analysis** (`documents.py`) | Two releases → reported / calculated / interpretation / gap, with char-offset citations | Filings, call transcripts, CIMs, investor updates. Swap the metric dictionary. |
| **Execution + journal** (`brokers/`, write-once journal) | Paper orders, plan adherence | Any decision record: an order, an IC memo, a comps table, a covenant test |

## Day trader
- **Data:** 1-minute bars and a quote stream (bid, ask, size, exchange timestamp). Same `Series` shape, at a finer resolution.
- **Calculations:** session VWAP (cumulative price × volume / volume from 09:30 ET); relative volume **by time of day** (volume so far today ÷ the average volume to this same minute over the previous N sessions, so the open isn't compared with lunch); spread in bps; quote age.
- **Rules:** e.g. "price reclaims VWAP with RVOL > 2 and spread < 5 bps". A **quote-freshness** rule returns *unknown* if the last quote is older than X seconds, just as a missing earnings date does today.
- **Execution:** the broker adapter already maps pending, partially filled, filled, canceled and rejected. Add a websocket order-update stream and an order monitor with time-in-force and auto-cancel. The duplicate-submission key carries over unchanged.

## Hedge fund analyst
- **Data:** filings, call transcripts and a positions table (shares, cost, sector, factor betas).
- **Document analysis:** run `compare()` on consecutive 10-Qs or transcripts. Each material change is tied to a stored **thesis pillar** (e.g. "gross margin expands with V50 mix") and marked *supports*, *contradicts* or *not addressed*, citing the passage.
- **Exposure:** the calculation core computes the position's weight, beta-adjusted exposure and the P&L sensitivity to the changed metric. The explanation layer says "this change touches 3.1% of NAV", with click-through inputs.
- **Journal:** the write-once table already stores what was known when the thesis was set, which is what a post-mortem needs.

## Investment banker: comparable companies
- **Data:** a `financials` table keyed by (company, metric, period, currency, definition, source document, page/offset).
- **Calculations:** EV/EBITDA, EV/Revenue and P/E, with calendarisation to a common fiscal year end and FX conversion at a stated rate and date.
- **Evidence:** every cell in the comps table is a `fact` with `inputs`, like the volume average today. Clicking it shows the source passage, the reporting period, the currency, and whether EBITDA is reported or adjusted. A cell with a missing definition shows *unknown* rather than being silently mixed in.

## Private equity analyst: downside debt coverage
- **Data:** income statement, cash flow statement and a debt schedule (tranches, rates, amortisation, covenants).
- **Calculations:** a scenario engine applies shocks (revenue −15%, margin −300 bps, SOFR +200 bps) and recomputes EBITDA, interest coverage, fixed-charge coverage and net leverage for each quarter.
- **Rules:** covenant tests become pass/fail/unknown rules ("net leverage ≤ 5.5×"), reported with margin to breach. That is the same table the swing evidence panel uses, with "near miss" meaning closest to a breach.
- **Documents:** cite the credit agreement's definition of EBITDA (the add-backs matter) from the document panel.

## Venture capital investor
- **Data:** monthly or quarterly investor updates, which are unstructured text.
- **Document analysis:** extend the metric dictionary to cash, monthly burn, runway, revenue/ARR, retention (logo and net dollar), headcount and named milestones. Compare update N with N−1.
- **Calculations:** compute runway as cash ÷ net burn and **check it against the founder's stated runway**. If the two disagree, show both. Flag milestones that were promised in N−1 and are absent from N as *gap*, using the same "not explained in source" treatment as today.
- **Journal:** record the investment thesis and the metrics the investor committed to watching. Each new update is then reviewed against it.
