# Roadmap: from prototype to a broker-connected platform

Tickmark today has:
- versioned rules with cited evidence
- live Yahoo daily data, a forward test and a daily auto-refresh
- paper orders through a local simulator, or through Alpaca with keys kept in the keychain
- broker-side exits, reconciliation and pre-trade risk checks
- alerts, a paper-to-live gate, and a write-once journal

Each phase reuses the core (`rules.evaluate_at`, the broker adapter contract, the risk engine, the write-once journal) instead of replacing it.

## Phase 1: make the broker connection real (one trader). Complete, 2026-11-22

| # | Feature | What was built |
|---|---|---|
| 1 | ✅ **Connect-broker screen** | Broker tab: paste Alpaca keys, tested with a read-only account call before saving, stored in the OS keychain. Broker equity and buying power are shown and used for sizing. |
| 2 | ✅ **Broker-side order lifecycle** | Entry with a GTC stop leg. Fills and the stop leg sync into the journal. Planned exits go out as market-on-close at 3:40 PM ET (stop canceled first), with a late market exit if that's missed. Manual close. P&L and adherence come from broker fills. |
| 3 | ✅ **Reconciliation** | After every refresh and on demand: untracked positions, quantity mismatches, missing positions, **positions without a stop**, stale and unknown orders. In simulator mode, a self-check. |
| 4 | ✅ **Pre-trade risk engine** | Hard blocks (kill switch, account blocked, restricted, buying power, daily loss) and soft blocks with logged reasons (positions, portfolio heat, sector, pattern-day-trader count). Versioned limits and an append-only override log. |
| 5 | ✅ **Alerts** | In-app feed, plus desktop, Slack and email delivery. Severity filter, de-duplication, and delivery outside database transactions. |
| 6 | ✅ **Paper-to-live gate** | 20+ closed paper trades with at least 80% plan adherence, clean reconciliation, deliberate risk limits, separate live keys, a typed confirmation phrase, a type-the-ticker step per order, a separate real-money journal, and a red UI. |

**Not done, moved to Phase 2:** order events are still polled rather than streamed. Phase 1's Alpaca path is tested against a fake server and has read the real paper account, but it hasn't placed a real order yet.

## Phase 2: more brokers, real-time data, more trading styles

The goal is that most traders can connect **the broker they already use**, on reliable data, with strategies beyond the one pullback setup. Order matters: each step unlocks the next.

### 2.1 Broker connection framework (first). ✅ Built 2026-11-22
- **OAuth connect flow.** A generic "Connect with ‹broker›" button, a local callback, refresh tokens kept in the keychain with automatic renewal, and an alert before a token expires (Schwab's weekly re-login, E*TRADE's daily expiry).
- **Adapter test kit.** Every new adapter runs the same contract tests against its own fake server, like `tests/fake_alpaca.py` does today: entry with stop, fills, stop leg, market-on-close exit, cancel, positions, reconciliation.
- **Capabilities per broker** (paper account, attached stops, market-on-close, fractional shares). The UI and risk engine adapt; for example, a broker without attached stops gets a separate stop order and a reconciliation check that it exists.

Built: the adapter contract and factory (every broker call goes through them), `app/oauth.py` (sign-in, PKCE, keychain tokens, refresh, re-login alerts), the shared contract suite, and capabilities (market-on-close, real-money eligibility).

### 2.2 Broker adapters, by reach
1. ✅ **Interactive Brokers** (paper) through the Client Portal Gateway, built 2026-11-22 and tested against a fake gateway. Next: run it against a real paper gateway, and confirm brackets, positions and contract search against the gateway's own docs. A TWS socket adapter is a later option.
2. **Charles Schwab Trader API** (OAuth). The largest US retail base. It has no API paper account, so the gate requires paper trades in Tickmark's simulator first.
3. **Tradier** and **TradeStation** (OAuth, with sandbox or SIM accounts).
4. **SnapTrade aggregator** for Robinhood, Fidelity, Webull, Vanguard and others: holdings and order sync for the journal and reconciliation, with trading wherever the broker allows it.
5. E*TRADE and moomoo as demand dictates.

### 2.3 Real-time order events. ✅ Built 2026-11-22 (Alpaca websocket; IBKR fast polling; UI push via Server-Sent Events)
- Broker streams (Alpaca `trade_updates`, IBKR order status callbacks) replace polling, so stops, fills and exits are recorded within seconds and alerts fire immediately.
- Polling stays as the fallback and as the source for reconciliation.

### 2.4 Licensed market data. ✅ Daily bars built 2026-11-22 (Alpaca SIP, split handling, quality monitor); intraday bars and quotes still to do
- Replace the unofficial Yahoo client with a licensed feed (Alpaca market data, Polygon or Databento): daily and **intraday** bars, real-time quotes, and **corporate actions** (this fixes today's split-adjustment limitation).
- A data-quality monitor for gaps, stale quotes and outliers, feeding the existing "provider gap" and "stale" handling.

### 2.5 Strategy library and trading styles. ✅ Swing strategies and the day-trading module (VWAP reclaim, opening-range breakout, live and replay sessions, paper execution, PDT hard block) built 2026-11-22; options basics still to do
- More setups on the same rule engine and evidence model: **breakout**, **mean reversion**, **trend following**, **post-earnings drift**. Each has plain-language parsing, versioned rules and a forward test.
- **Day-trading module** ([EXPANSION.md](EXPANSION.md)): session VWAP, relative volume by time of day, quote freshness and spread checks, intraday stops, and order monitoring. Pattern-day-trader rules become hard blocks for small accounts.
- **Options basics:** covered calls and cash-secured puts on swing positions, with defined-risk checks.

### 2.6 Portfolio and execution analytics
- **Portfolio view:** exposure by sector and factor, correlation between open positions, value at risk, and an open-risk heat map. These feed the existing soft blocks.
- **Execution quality (TCA):** slippage against the decision price, fill rates, stop gaps, and broker comparison. It shows whether the plan's assumed fills are realistic.

**Phase 2 is done when** a trader on IBKR, Schwab or Alpaca can connect in under two minutes, every adapter passes the shared contract tests, order events arrive in real time, and at least three strategy types run with forward tests on licensed data.

## Phase 3: firms (fintech, hedge funds, PE/VC, banks)

- **Multi-user and roles:** SSO login (OIDC), roles for trader, portfolio manager, risk, compliance and viewer, per-account permissions, and **four-eyes approval** above a size threshold.
- **Books and records:** Postgres with migrations, a write-once audit log of every rule change, order, override and approval (SEC 17a-4 style retention), and exports for examiners.
- **Post-trade compliance:** restricted-list and wash-sale surveillance across accounts, and override pattern analysis.
- **Institutional connectivity:** FIX 4.2/4.4 sessions to prime and executing brokers, or integration with the firm's EMS (for example Bloomberg EMSX).
- **Platform API:** REST plus webhooks so fintech apps can embed screening, evidence and journaling, with white-label theming.
- **Stakeholder workspaces** ([EXPANSION.md](EXPANSION.md)): hedge fund thesis tracking against filings, a sourced comps table for bankers, downside covenant tests for PE, and investor-update parsing for VC.
- **Deployment:** Docker, a job queue for refreshes and alerts, monitoring, and secrets in a vault.

## Recommended next build

1. **Real-account checks** of what's built: one Alpaca paper order, and an IBKR paper gateway session (log in, Check connection, one small order).
2. **Phase 2.2.2-2.2.3:** Schwab, Tradier and TradeStation adapters on top of the sign-in framework, once their app registrations exist.
3. Day trading follow-ups: ✅ Alpaca Paper brackets with a 15:55 flatten and a server-side day monitor (2026-11-22, fake-server tested). Still to do: a first real Alpaca paper day trade, IBKR brackets, and historical quotes for replay.
4. ✅ Copilot (2026-11-22): Claude chat that reads the desk and drafts proposals you apply; tested with a fake client. Next: run it with a real key, stream replies, and add proposal analytics (how often proposals were applied and how they worked out).
4b. ✅ 2026-11-22: crypto in the journal and P&L stats, per-account real-money switches (Alpaca, IBKR, OKX/Coinbase/Kraken, Robinhood Crypto), the multi-user portal, and Tickmark as an MCP server. Next: run each live venue once with real keys and small size; OAuth sign-in for MCP clients; Postgres and a KMS for hosting; SnapTrade for read-only Fidelity and Robinhood stocks.
5. Phase 2.6: the portfolio view (exposure, correlation, VaR), and execution analytics comparing real fills with the plan.
6. IBKR's websocket order topic, replacing its 10 s polling.
