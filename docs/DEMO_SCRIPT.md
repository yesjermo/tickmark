# Two-minute demo script

Start from a fresh state: click **Reset demo** in the top bar (or run `./run.sh` on a new checkout). The view should be in Beginner mode.

**0:00 – Orientation (10 s)**
"This is SwingDesk. Top bar: we're in **paper** mode on the local simulator, and every price here is **simulated**. Data runs through the 2026-11-20 close."

**0:10 – Plain English to explicit rules (25 s)**
Strategy tab. Point at the sentence: *pulled back to the 9-day EMA on lighter volume… flag earnings before my planned exit.*
"Every vague word gets a definition. 'Lighter' means below 80% of the prior 20 sessions' average. 'Planned exit' means five sessions. And I choose whether earnings exclude a stock or just warn."
Leave earnings on **warn**. Click **Confirm and save as v1**.

**0:35 – Why NVDA qualified (25 s)**
Watchlist: NVDA *Qualifies*, SNDK *Near miss*, IREN *Missing info*, COIN *No setup*.
Click NVDA. Beginner view: "It dipped to its recent average, finished above it, and fewer shares traded than usual."
Toggle **Analyst**: "Same verdict, same numbers: the candle, EMA 167.16, volume ratio 62%, thresholds and the data timestamp." Click the average volume: "These are the 20 sessions behind it, and the signal day is excluded."

**1:00 – Why the others dropped off (10 s)**
Click SNDK: "One reason: volume at 86%, and the threshold is under 80%. Earnings on the 24th are flagged too." Click IREN: "We don't know its next earnings date, so it's marked unknown instead of being treated as safe."

**1:10 – Act on it (20 s)**
Back to NVDA → **Order ticket** → **Open order preview**.
"118 shares, entry assumption $168.10 (last close; it actually fills at the next open), stop $164.10, about $19.8K of position, about $492 lost if stopped, inside my $500 budget. Fills can differ."
**Review and confirm…** → double-click **Confirm paper order**. "Double-clicking still creates one order. The preview id is the idempotency key."

**1:30 – What happened next (20 s)**
Click **Advance one session** five times. Toast messages: filled at the 11-23 open, planned exit at the 11-30 close.
Journal tab: "Here's what I knew at entry, locked: thesis, rules v1, the evidence table, the data timestamp. Here's what happened: +$1,061.72, and I **followed the plan** 4 of 4."

**1:50 – Is the rule any good? Test it forward, live (10 s)**
Forward test tab: "Every qualifying setup is written down when it's seen, with its evidence, and then followed on real prices. Nothing is back-filled." Click **Live data** in the top bar: "Now it's real Yahoo bars. Nothing qualifies today, so the live record starts empty and grows each session I refresh. My paper orders fill on real opens."

*(Optional extra 20 s: Documents tab → **Analyze**. The gross-margin drop is marked "not explained in source", the investor day on 2026-11-30 falls inside the holding window, and clicking a citation highlights the exact sentence.)*
