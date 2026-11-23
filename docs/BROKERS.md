# Connecting brokers

Tickmark talks to every broker through one adapter contract: submit an entry with an attached stop, sell, get or cancel an order, list open orders, positions, and the account. The rules, risk checks, journal, reconciliation and alerts don't change from broker to broker. What changes is **how the user connects**, and brokers fall into five patterns.

| Pattern | How the user connects | Who uses it | In Tickmark |
|---|---|---|---|
| **API key** | Paste a key ID and secret once | API-first brokers (Alpaca) | **Available** |
| **OAuth sign-in** | Click *Connect with ‹broker›*, log in on the broker's own site, approve. Tokens refresh automatically. | Schwab, Tradier, TradeStation, E*TRADE (OAuth 1.0a) | **Sign-in framework built**; order routing in Phase 2 |
| **Local gateway** | A small broker program runs on your computer, you log in there, and Tickmark connects to it | Interactive Brokers (Client Portal Gateway), moomoo (OpenD) | **IBKR available (paper)**; moomoo Phase 2 |
| **Aggregator** | One consent screen links brokers that have no public trading API | SnapTrade (Robinhood, Fidelity, Webull, Vanguard and others); Plaid Investments (read-only) | Phase 2 |
| **FIX session** | The firm's ops team sets up a FIX connection with the broker, certified in the broker's test environment | Hedge funds, asset managers, prop desks | Phase 3 |

"Automatic" here means the user never copies tokens around. For OAuth and aggregators they click one button and log in at the broker. For gateways they keep the broker's program running. In every case, credentials and tokens stay server-side in the system keychain (`app/credentials.py`), and the browser only sees a masked hint.

The Broker tab lists all of these, with their status, under **Other brokers**.

---

## Available now

### Alpaca (API key)
*Retail and API-first; US stocks, ETFs, options and crypto; free paper accounts.*

1. Sign in to Alpaca, switch to **Paper Trading** and generate API keys.
2. In Tickmark switch to **Live data**, then open **Broker** and paste both keys. Click **Test and save**: Tickmark reads the account before it saves anything.
3. Click **Send orders to Alpaca**.

Tickmark then:
- sizes orders from Alpaca's equity and buying power
- sends entries with a GTC stop (an OTO order)
- syncs fills and the stop leg
- sends planned exits as market-on-close orders at 3:40 PM ET, canceling the stop first
- reconciles positions against the journal after every refresh

Real money is a separate key pair behind the paper-to-live gate (see below).

---

### Interactive Brokers (local gateway): available for paper accounts
*Active traders and professionals; global stocks, options, futures, FX and bonds.*

1. Download IBKR's **Client Portal Gateway**, unzip it and run `bin/run.sh root/conf.yaml`.
2. Open `https://localhost:5000` in a browser and log in with your **paper** username. IBKR handles the password and two-factor; Tickmark never sees or stores them.
3. In Tickmark switch to **Live data**, then go to **Broker → Interactive Brokers → Check connection**, and click **Use** next to your paper account (IDs start with `DU`).
4. Click **Send orders to Interactive Brokers**.

**macOS note:** port 5000 is usually taken by **AirPlay Receiver**. Either turn it off (System Settings → General → AirDrop & Handoff), or set `listenPort` in the gateway's `root/conf.yaml` to another port (for example 5001) and change the Gateway URL in Tickmark to match. Tickmark detects the clash and tells you.

**How it trades:**
- entries go out as a parent order plus a GTC stop child
- IBKR's order-confirmation prompts are acknowledged automatically
- fills and the stop sync from the gateway, and reconciliation compares IBKR positions with the journal

IBKR's Web API has no confirmed market-on-close order, so **planned exits are market orders sent at 3:40 PM ET**: near the close, not at the close.

**Limits in this build:**
- **Paper accounts only.** Non-`DU` accounts are refused, and IBKR can't be used behind the real-money gate.
- **Not yet run against a real IBKR gateway.** It was built from IBKR's published Trading Web API page; the order body, confirmation step, cancel, live-order fields and ledger were confirmed there. Brackets, positions and contract search weren't documented on that page and are assumed. All of it is tested against a fake gateway.
- The gateway session times out and needs a periodic re-login in the browser.

## Planned (Phase 2): how each will connect

### Interactive Brokers TWS / IB Gateway socket (later option)
- For traders who already run Trader Workstation: a socket adapter on port **7497** (paper) or **7496** (live), using the same contract.

### Sign-in framework (built)
The **Sign-in brokers** panel in the Broker tab handles OAuth for Schwab, Tradier and TradeStation:
- **Connect** opens the broker's login page, protected by a one-time state value and, where supported, PKCE.
- The callback swaps the code for tokens, which go into the keychain.
- Access tokens refresh automatically, and an alert fires a day before a forced re-login (Schwab, weekly).

Each provider needs the app registered with that broker, and its client ID set on the server (`SCHWAB_CLIENT_ID`, `TRADIER_CLIENT_ID`, `TRADESTATION_CLIENT_ID`, plus the matching `_SECRET`). Until then, its button explains what's missing. Order routing for these brokers comes with their adapters. The framework has been tested against a fake token server only.

### Charles Schwab (OAuth)
*The largest US retail base, including former TD Ameritrade clients; US stocks, ETFs and options.*

- **How it will work:** click **Connect with Schwab**, log in on Schwab's site and approve Tickmark. Tickmark stores the refresh token in the keychain and renews access tokens automatically.
- **Setup once:** Tickmark, or whoever deploys it, registers an app on Schwab's developer portal and is approved by Schwab.
- **Things to know:** Schwab's refresh token expires about every **7 days**, so the user has to log in again weekly. Tickmark will remind you through an alert. The API has **no paper trading**, so paper testing stays in Tickmark's simulator, and the paper-to-live gate still applies.

### Tradier (OAuth or token)
*Retail and active options traders; free **sandbox** for paper.*

- Connect with Tradier (OAuth), or paste a personal access token from the dashboard. Sandbox and live use different base URLs, which maps neatly onto Tickmark's paper and real environments.

### TradeStation (OAuth)
*Active traders; stocks, options and futures; **SIM** accounts for paper.*

- Connect with TradeStation (OAuth). The SIM and live accounts appear under the same login, and Tickmark shows which one each order goes to.

### E*TRADE (OAuth 1.0a)
*Retail; sandbox available.*

- Connect with E*TRADE. It uses the older OAuth 1.0a flow, and access tokens expire at midnight US Eastern, so a daily re-authorization prompt is expected.

### moomoo / Futu (local gateway)
*Retail in the US and Asia; paper environment available.*

- Run the **OpenD** gateway, log in, and Tickmark connects to it locally, as with IBKR.

### Robinhood, Fidelity, Webull, Vanguard and others (aggregator)
*Very large retail user bases, but no general public trading API for stocks.*

- **How it will work:** one **SnapTrade** connection screen lets the user pick their broker and log in there. Holdings, balances and orders sync into Tickmark for the journal and reconciliation.
- **Trading** is available only where that broker permits it through the aggregator. Fidelity and Vanguard are typically **read-only**, so Tickmark would screen and journal, and you'd place the order at the broker yourself.
- **Plaid Investments** is the read-only alternative for holdings and transactions.

---

## Professional and institutional (Phase 3)

### FIX (prime and executing brokers)
- Firms connect over **FIX 4.2 or 4.4** with a sender comp ID and target comp ID, certified in the broker's UAT environment first. Tickmark would run a FIX engine (such as QuickFIX) and map its order lifecycle to execution reports.
- Many funds instead route through an **EMS** they already use (Bloomberg EMSX, FlexTrade, and similar). In that case Tickmark integrates with the EMS rather than with each broker.
- Multi-user roles, four-eyes approval, books and records, and post-trade compliance (Phase 3) come before this, because firms need them first.

---

## Paper-to-live gate (all brokers)

Real money is opt-in and has to be earned. It unlocks only when, on the paper record in Live data mode:
- **20 or more** paper trades have closed, with the plan followed on **at least 80%** of them
- the last reconciliation was **clean**
- risk limits have been **saved deliberately** (version 1 or later) and the **kill switch is off**

Then you add **separate live keys** (tested with a read-only call) and type the confirmation phrase. In real-money mode:
- the top bar and the ticket turn red
- every order requires typing the ticker
- trades go to their own journal (`swingdesk-real-money.db`)
- switching back to paper is one click, and switching to Demo data turns real money off

The real-money path has only been tested against the fake broker server. It has never been run against a live account.
