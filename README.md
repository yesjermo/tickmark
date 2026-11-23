# Tickmark

A research and trading desk that shows its reasons. You describe a setup in plain English, Tickmark turns it into rules you can read, screens your watchlist on real prices, shows the evidence behind every pass and fail, runs pre-trade risk checks, and records every trade in a journal that can't be rewritten. An AI copilot can research strategies and draft orders, but nothing happens until you click.

- **Live app:** https://tickmark-yqj2.onrender.com (open to everyone; visitors start as guests)
- **Project page:** https://yesjermo.github.io/tickmark-site/
- **Install it as an app:** Chrome/Edge install icon in the top bar; iPhone Share → Add to Home Screen; Safari on Mac File → Add to Dock.

This repository is private. Tickmark is decision-support software, not investment advice.

## What's in it

| Area | What you get |
|---|---|
| **Portfolio (home)** | Total balance across the trading account and linked accounts, cash, buying power, unrealized and realized P&L, win rate, a graph of total value over time, allocation and positions. Fits one screen on desktop; long lists scroll inside their block. |
| **Charts** | Tickmark's chart (completed bars, rule markers, planned stop) or TradingView's chart. Candle size 5m / 15m / 1h / 1D / 1W / 1M and range 1M–2Y; intraday candles use TradingView. A TradingView ticker tape of the watchlist with SPY and QQQ. |
| **Rules and evidence** | Five swing strategies and two intraday ones on one engine; each rule shows its threshold, what was observed, and pass/fail, for beginners or analysts. |
| **Workspaces** | Backtest (simulated), Live test (real prices, paper orders), Live trading (real money, per account). |
| **TradingView** | Sign in to your own TradingView on TradingView's page, open your saved layouts on the symbol you're viewing ("My TradingView" on the chart), and receive your alerts through a private webhook. Signals never place orders. |
| **Connections** | Every broker, exchange and AI is a tile that opens its own connect window: sign in (OAuth, API keys, gateway), then switch real-money trading on in the same window. |
| **Copilot** | Claude, GPT, Gemini or free open models (Gemma 4, Qwen 3.5, a small OpenAI gpt-oss). Researches strategies and drafts rules, orders and exits for you to approve. |
| **Accounts** | Sign up with email or **Continue with GitHub** (no server key needed); link GitHub and TradingView in Account. Guests get a view-only desk on real prices with paper trading; sign-up keeps it. "Remember me" keeps a browser signed in for 30 days. Per-user encrypted keys; data backed up to a private GitHub repo. |
| **Appearance** | System (default), Light or Dark, and color themes: Tickmark Stone plus IDE themes (GitHub, One Dark/Light, Dracula/Alucard, Nord, Solarized, Monokai), each in light and dark. |
| **Motion and comfort** | Smooth (inertia) scrolling, cards that ease in as they scroll into view, motion Full / Subtle / Off, focus (ADHD-friendly) mode, generated background music, custom cursor. |
| **Safety** | Paper first, a kill switch, stops at the broker, a typed phrase before any account trades real money and the ticker typed for every live order. |

## Contents

[Quick start](#quick-start) · [Live mode](#live-mode) · [Copilot](#copilot-copilot-tab) · [Copilot plans](#copilot-plans-claude-gpt-and-gemini-free-with-sign-in-or-on-your-own-key) · [Connections](#connections-page-sign-in-to-brokers-exchanges-and-ai) · [Interface](#interface) · [Multi-user portal](#multi-user-portal-hosted) · [Hosting on Render](#hosting-on-render-public-open-sign-up) · [Brokers](#brokers-at-a-glance) · [Risk checks](#pre-trade-risk-checks) · [Day trading](#day-trading-day-trading-tab) · [Market data](#market-data-yahoo-free-or-alpaca-sip-licensed) · [Real money](#real-money-per-account-live-switch) · [What was tested](#what-was-exercised-and-what-was-only-checked-with-fixtures) · [Audit fixes](#audit-fixes-2026-11-23) · [Known limitations](#known-limitations)

## How it started

A broker-connected research assistant for a US equity swing trader, inspired by HKUST's InvestLM, built around one question:

> **Why did this stock qualify under my rules, what evidence supports that answer, and what happened after I acted on it?**

Data modes: **live data** (real completed daily bars and earnings dates from Yahoo Finance, or Alpaca's licensed SIP feed with a key) and **demo data** (seeded simulated prices with a manual session clock, for a two-minute walkthrough). Neither needs API keys. Real money needs a broker connection and that account's own switch.

## Quick start

Requires Python 3.11+ and Node 18+.

```bash
./run.sh
```

Then open http://localhost:8000. The first run creates `backend/.venv`, installs npm packages, builds the UI, and seeds `backend/data/swingdesk.db`.

For hot reload during development (API on :8000, UI on :5173):

```bash
./run.sh dev
```

Tests:

```bash
cd backend && .venv/bin/python -m pytest -q
```

### Manual setup

```bash
cd backend && python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000          # API (+ built UI if frontend/dist exists)
cd ../frontend && npm install && npm run dev        # UI on :5173, proxies /api to :8000
```

### Optional integrations (server-side env vars only)

| Variable | Effect |
|---|---|
| `ALPACA_API_KEY_ID`, `ALPACA_API_SECRET_KEY` | Enables the **Alpaca Paper** broker mode in the top bar. The adapter refuses any non-paper URL. |
| `SWINGDESK_LLM=claude` (+ Anthropic credentials) | Adds a Claude-written narrative to explanations. Replies containing any number that isn't in the computed facts are thrown out, and the deterministic text is shown instead. |
| `SWINGDESK_DB` | Path to the SQLite file. |

Credentials are read only by the FastAPI process. The browser never receives them.

## Live mode

Click **Live data** in the top bar. The first switch fetches about two years of daily bars for each watchlist ticker.

- **Completed bars only.** Today's bar is ignored until 16:15 ET, so a signal is never based on a session that's still trading.
- **Auto-refresh (on by default)** runs at 16:20 ET every trading day while the server is running, so the forward test records every session and paper orders fill without anyone clicking. It waits against the wall clock, so a laptop that sleeps through 16:20 refreshes when it wakes. It also catches up on startup if the data is behind, and retries a failed fetch every 15 minutes, up to 3 times. Turn it off with the **Auto** checkbox in the top bar. The top bar shows the next run and what triggered the last refresh.
- **When the server isn't running**, schedule the one-shot refresh yourself, for example with cron (weekdays at 16:20 ET; adjust for your timezone):

  ```
  20 16 * * 1-5  cd ~/Documents/swing-desk/backend && .venv/bin/python -m app.refresh_cli >> data/refresh.log 2>&1
  ```

  It exits with 0 on success, 1 if the app is in demo mode, and 2 if any ticker failed. Running it while the server is also running is safe: sessions are only processed once.

  **Installed on this Mac (2026-11-22)** as `20 13 * * 1-5` (13:20 Pacific = 16:20 ET; both zones change clocks on the same dates). Output goes to `backend/data/refresh.log`. To see it, run `crontab -l`. To remove it, run `crontab -e` and delete the Tickmark lines.
- **Refresh live data** pulls new sessions on demand. Each new session runs through the paper simulator in order: pending entries fill at that day's real open, stops check that day's real low, and planned exits use the real close.
- **Editable watchlist** (1–12 tickers). Add or remove symbols in the left panel.
- **Provider problems are shown, not hidden.** A ticker whose fetch fails keeps its cached bars, is marked *stale*, and drops to *Missing info*. A bar Yahoo returns without a close (this happened to SNDK on 2026-11-20 during testing) is labelled "provider gap" rather than filled in.
- Future earnings dates from Yahoo can be estimates. The source note says so.
- The live database (`backend/data/swingdesk-live.db`) is separate from the demo one, so live paper trades never mix with demo trades.

## Copilot (Copilot tab)

A chat assistant that reads the desk and drafts changes for you to approve. You pick the model:

| Model | Price per M tokens (in / out) | Best for |
|---|---|---|
| Claude Fable 5.2 | $8 / $40 | hardest multi-step decisions; strongest reasoning, slowest |
| **Claude Opus 5.5** (default) | $4 / $20 | strong judgement and tool use at a moderate price |
| Claude Sonnet 5.5 | $2 / $10 | everyday lookups and reviews, faster |
| Claude Haiku 5.5 | $0.10 / $0.50 | quick simple questions; weaker at multi-step decisions |
| Local model (e.g. Qwen 3.5 in Ollama) | free, runs on your Mac | private use with no API key; good at single lookups, weak at judgement |

**Local models.** Any OpenAI-compatible server works: Ollama (default URL `http://localhost:11434/v1`), LM Studio (`http://localhost:1234/v1`), llama.cpp or vLLM. Choose "Local model" in the Copilot tab. *Check server* lists the installed models. To get Qwen 3.5 9B, run `ollama pull qwen3.5:9b` (several GB).

Local models get the same tools and guardrails as Claude. When the server is on this Mac, nothing leaves the machine. Switching between Claude and a local model starts a new conversation, because the two keep history in different formats.

**Checked on real data (2026-11-22)** with `qwen3.5:4b` in Ollama on this Mac:
- It called the indicator tool and quoted RSI14 15.05 and SMA50 207.0 correctly, in about 27 s.
- But it got the gap wrong ("~92 points"; the real gap is 81.47) and misused "divergence".
- The indicator tool now returns those distances already computed. On the same question it answered "$81.47 below, 39.36%", which is right.
- Small models still miss judgement calls, so use Claude for decisions.

**4B vs 9B, same two questions** (Ollama on this Mac, read-only):

| | qwen3.5:4b | qwen3.5:9b |
|---|---|---|
| NVDA vs 50-day SMA | correct ($81.47, 39.36%), 34 s | correct ($81.47, −39.36%), 44 s |
| Which symbols qualify, and why not | looked up 4 of 5 symbols, and every number it gave was correct. It **guessed AAPL** without looking, and its summary contradicted its own table ("far above the EMA"; they're below). 112 s | looked up only NVDA and **gave NVDA's numbers for all five**. Verdict right, details wrong. 62 s |

Neither is reliable for decisions that compare several symbols. The 4B was more thorough but slower; the 9B was quicker but over-generalized. For decisions, use a Claude model.

**Checked with `qwen3.5:9b`** (pulled 2026-11-22, 7.6 GB):
- "How far is NVDA below its 50-day SMA?" gave $81.47 and −39.36%, which is correct (44 s).
- "Which symbols qualify, and why not?" was right on the verdict: none qualify, and all five fail the close-above-EMA and lighter-volume rules (62 s).
- But it looked up only NVDA's evidence and gave NVDA's numbers (EMA 155.09, about 2.4× volume) as if they applied to all five. The real volume ratios are 121–243%.
- So it's fine for lookups, but check anything that compares several symbols, or use Claude.

**Setup for Claude:** open the Copilot tab and paste your own Anthropic API key. It's checked with a free API call, then stored in the system keychain. You can also set `ANTHROPIC_API_KEY` before starting the server. Anthropic bills you for each question, usually a few cents. Questions, and the desk data the copilot looks up, go to Anthropic's API. Broker keys never do.

**What it reads.** It uses the same code as the rest of the app, so the numbers it quotes come from tool results, not from memory:
- the overview and the watchlist screen
- the rule evidence for each symbol
- indicators: EMA 9/20/50, SMA 50/200, RSI 2/14, ATR, MACD 12/26/9, Bollinger 20/2 with %B, 20-day and 52-week ranges
- the strategy library
- positions and orders
- the journal, risk limits and the forward test
- the day-trading session

**What it can propose.** Each proposal shows up as a card. Nothing changes until you click:

| Proposal | What your click does |
|---|---|
| Rule change | saves a new rule version, but only if the rules haven't changed since the proposal |
| Risk limits | saves a new limits version, under the same staleness check |
| Swing order | opens the Ticket with its size and stop; you confirm there as usual, with risk checks, overrides and the live-money phrase |
| Day trade | opens the Day trading tab; you confirm there |
| Exit (close, cancel, flatten) | sends it through the normal close, cancel or flatten path, after a confirm dialog |

**What it can never do.** No tool can:
- submit or cancel an order directly
- override a risk block or touch the kill switch
- change the broker mode, the environment or the live-money gate

A test checks the tool list for these. Rule and limit changes the copilot proposed are logged as copilot proposals.

**Technical details:**
- Messages API with a tool loop the app runs itself: adaptive thinking at effort `medium` (set `SWINGDESK_COPILOT_EFFORT` to change it).
- Prompt caching covers the tools, the system prompt and the history. The server-side refusal fallback is on.
- Conversations are stored in SQLite (`copilot_threads`, `copilot_proposals`), separately for each data mode.
- It's decision support, not financial advice.

## Linked accounts (read-only): crypto exchanges, Robinhood Crypto, moomoo/Futu, Phantom

In the Broker tab, **Linked accounts** lets Tickmark *read* balances and positions from other places. The copilot sees them through `get_linked_accounts`, and there's a combined USD total. No order code exists for these. Orders still go only to the trading broker (simulator, Alpaca or IBKR).

| Provider | How it connects | Test account | Checked from this Mac (2026-11-22) |
|---|---|---|---|
| Coinbase | Advanced Trade (CDP) key with View permission, via ccxt | none: reads the real account | public price API reachable |
| Kraken | API key with Query Funds only, via ccxt | none: reads the real account | public price API reachable |
| OKX | key + secret + passphrase, via ccxt | Demo trading (default) | public and demo APIs reachable |
| Binance.US | read-only key, via ccxt | testnet available | public price API reachable |
| Binance.com | read-only key, via ccxt | testnet.binance.vision | **blocked from the US (HTTP 451), testnet too** |
| Bybit | read-only key, via ccxt | testnet.bybit.com | **blocked from the US (HTTP 403), testnet too** |
| Robinhood Crypto | official Crypto Trading API, Ed25519-signed requests | none | not run (needs your key) |
| moomoo / Futu | local OpenD gateway + `futu-api`/`moomoo-api` SDK | paper ("simulate") by default | not run (needs OpenD) |
| Phantom (Solana) | **public address only**, watch-only, via a Solana RPC | n/a | not run (needs your address) |

**What's not linkable, and why:**
- **Robinhood stocks and options:** no public API. Unofficial APIs break Robinhood's terms.
- **Fidelity:** no public trading API.
- **Webull:** its OpenAPI needs an application approved by Webull.

All three are listed as planned through the SnapTrade aggregator. **Phantom** is a self-custody wallet with no broker API, so it's watch-only. Tickmark never asks for a seed phrase or private key.

**Security:**
- Secrets go in the system keychain. The browser only ever sees non-secret fields.
- Every link is tested before anything is saved.
- Use read-only or view-only keys for real accounts.
- Pricing uses each exchange's public ticker (`X/USD`, then `/USDT`, then `/USDC`). Holdings with no price show "—" and aren't counted in the total.

## Crypto orders (Crypto tab)

Spot crypto orders go to a linked exchange account: a **testnet or demo** account right away, or a **real** account (OKX, Coinbase, Kraken, Robinhood Crypto) once you've turned on its real-money switch (see the per-account live switch). Real-money tickets turn red, need the coin typed to confirm, and are blocked once that account's realized loss today reaches the daily loss limit.

**Robinhood Crypto** goes through its official API with Ed25519-signed requests. Each preview id maps to a fixed UUID, so a retry can't create a second order. It has been tested against a fake server only.

**From this Mac, only OKX demo trading is reachable.** Binance.com's testnet (which Binance.US also uses) returns HTTP 451 here, and Bybit's returns 403.

**Preview.** Uses the exchange's live bid and ask (buys are priced at the ask), its amount precision and its minimum amount and value. Hard checks, with no overrides:
- the kill switch is off
- the asset isn't on the restricted list
- there's enough free balance (1% buffer)
- the order is at most 25% of the account's priced value
- the stop is below the entry

Previews expire after 2 minutes.

**Submit.** Needs an explicit confirm. The preview id is the client order id, which blocks duplicates both locally and at the exchange. The order row is reserved first, then the exchange is called outside the database transaction.

**Stops.** Once a buy with a stop has filled, Tickmark places a stop sell for the filled amount at the exchange. It's placed only once. If the exchange refuses it, a critical alert says the position has no stop.

**Close and cancel.** **Close** cancels the stop first, then sells the filled amount at market. **Cancel** cancels a working order.

**Sync.** Every 15 s while the Crypto tab is open, and every 30 s in the background around the clock, since crypto never closes.

**Copilot.** `get_crypto_orders` reads these orders, and `propose_crypto_order` drafts one. The draft opens in the Crypto tab, where you confirm. The copilot never sends one itself.

**Not yet:**
- Kraken's validate-only dry run
- crypto in the journal and P&L stats
- crypto strategies on 24/7 bars

## Workspaces: Backtest, Live test, Live trading

The top bar switches between three workspaces. Each keeps its own record, and a colored line along the top shows which one you're in.

| Workspace | What it is | Money |
|---|---|---|
| **Backtest** (violet) | The demo account: simulated sessions you step through with **Next session**, plus the historical check and day-trading replays | none |
| **Live test** (amber) | Paper trading on real completed bars: the forward test of your rules, run with the local simulator or a broker's paper account | none |
| **Live trading** (red) | Real orders, on accounts whose real-money switch you turned on (see the per-account live switch) | real |

Switching never deletes anything: the backtest and live-test databases are kept separately. **Live trading** opens the Broker tab if no real-money switch is on yet.

## Copilot plans: Claude, GPT and Gemini, free with sign-in or on your own key

The copilot works like an IDE copilot's free tier:
1. Clients and users **sign in to their Tickmark account**. On a hosted server, sign-in is required: the server refuses copilot requests from anyone not signed in.
2. They pick **Claude, GPT or Gemini** in the Copilot tab's model picker.
3. They get a monthly allowance, with **no API key needed**: the server's own keys pay for it, and each signed-in user has their own meter.

Anyone who wants more uses their own key and gets unlimited use.

**Who can see what:**
- **Not signed in:** the hosted server refuses every request, so no copilot at all.
- **Signed in:** the free Claude, GPT and Gemini plans the server offers, each with a meter under the model picker. With no key of their own, users start on the first free plan offered.
- **Own key added:** any Claude model, any GPT or Gemini model that key can use, or a local model. Unlimited.

| Plan | Who pays | Limit | Server setting |
|---|---|---|---|
| **Free · Claude** | the server operator's Anthropic key | `SWINGDESK_FREE_MESSAGES` per user per month (default 50) | `SWINGDESK_FREE_TIER=on` + `ANTHROPIC_API_KEY`; model `SWINGDESK_FREE_MODEL` (default claude-opus-5-5) |
| **Free · GPT** | the server operator's OpenAI key | `SWINGDESK_FREE_GPT_MESSAGES` per user per month (default 50) | `SWINGDESK_FREE_TIER=on` + `OPENAI_API_KEY`; model `SWINGDESK_FREE_GPT_MODEL`, or left empty to use the newest flagship the key can access |
| **Free · Gemini** | the server operator's Gemini key | `SWINGDESK_FREE_GEMINI_MESSAGES` per user per month (default 100) | `SWINGDESK_FREE_TIER=on` + `GEMINI_API_KEY` (or `GOOGLE_API_KEY`); model `SWINGDESK_FREE_GEMINI_MODEL`, or the newest flagship |
| **Open-source · free** | the operator hosts the model | fair use, `SWINGDESK_OSS_DAILY` per day (default 300) | `SWINGDESK_OSS_BASE_URL` (Ollama, vLLM, llama.cpp: any OpenAI-compatible server) + `SWINGDESK_OSS_MODEL` |
| Claude, your own key | you, through Anthropic | unlimited | Copilot tab → paste key |
| **OpenAI GPT**, your own key | you, through OpenAI | unlimited | Copilot tab → OpenAI GPT → paste key (platform.openai.com) |
| **Google Gemini**, your own key | you, through Google | unlimited | Copilot tab → Google Gemini → paste key (aistudio.google.com) |
| Your own local model | free, on your machine | unlimited | Copilot tab → Local model |

**GPT and Gemini** run through each provider's OpenAI-compatible chat endpoint (`api.openai.com/v1`, and Gemini's `/v1beta/openai`), with the same tools and guardrails as Claude:
- **Key check:** the key is checked with a free model-list call, then stored like the other keys.
- **Model picker:** lists the chat models that key can actually use, so new model names appear without a code change. The newest flagship is selected by default.
- **Gemini schemas:** Gemini receives the tool schemas converted to the subset it accepts (no `additionalProperties`, `nullable` instead of type lists).
- **Switching models:** GPT, Gemini, open-source and local models share one conversation format, so you can switch between them mid-conversation; switching to or from Claude starts a new conversation.
- **Testing so far:** tested against fake OpenAI and Gemini servers. Both real endpoints were confirmed reachable (they reject a missing or dummy key as expected), but no real GPT or Gemini call has been made, because that needs your key.

**Why a free plan says "not offered here".** Someone has to pay for, or run, every free plan. A plan only appears once the server has what it needs: the operator's API key for that provider (free Claude, GPT or Gemini), or a reachable open-source model server (the open-source plan). Ways to offer free plans at no cost:
- **On your own computer:** point the open-source plan at Ollama. Set `SWINGDESK_OSS_BASE_URL=http://localhost:11434/v1` and list the models with `SWINGDESK_OSS_MODELS=gemma4:e2b,gemma4:12b,qwen3.5:4b` (users pick one in the Copilot tab), and keep the Ollama app running.
  - **Small models that fit a 16 GB Mac** (checked 2026-11-23 on an M2): Google Gemma 4 E2B (about 20 s per answer, correct), Gemma 4 12B (about 47 s, correct), Qwen 3.5 4B and 9B.
  - **OpenAI gpt-oss, small version:** the full gpt-oss 20B (13 GB) pushed free memory to about 5% and macOS killed the API server, so the desk offers **gpt-oss nano 6B** instead: a community-pruned gpt-oss 20B (4.6 GB, `DedeProGames/gpt-oss-nano:6b`) rebuilt locally with the official gpt-oss chat template as `gpt-oss-nano:6b`. It's pruned rather than distilled; it's the smallest real gpt-oss build that exists. Set it up:
    ```bash
    ollama pull DedeProGames/gpt-oss-nano:6b
    ollama show --modelfile gpt-oss:20b | sed -E 's|^FROM .*|FROM DedeProGames/gpt-oss-nano:6b|' > nano.Modelfile
    ollama create gpt-oss-nano:6b -f nano.Modelfile
    ```
    (The second line needs the full `gpt-oss:20b` downloaded once, only for its chat template.)
  - **Blank Yahoo closes:** Yahoo's daily history sometimes leaves the newest close empty (SNDK on 2026-11-20) while its quote has it. The loader now fills that close from the regular-market quote once the session is complete and labels the source; without a quote it stays missing, never zero. The watchlist's latest-price column falls back to Yahoo quotes when there's no Alpaca key, so it works on the hosted site too.
  - **Snapshot mode:** in tests on 2026-11-23, the nano model never called a tool (three of three tries). Once it answered "yes, SNDK qualifies" with no data, which was wrong. So it runs in snapshot mode: the copilot fetches the desk's real data first (overview, positions, and evidence for any symbol named in the question), hands it to the model with no tools, and tells it to answer only from that data. Asked "Does SNDK qualify today, and why?", it answered NO_SETUP and named the two failing rules (close 79.24 not above the EMA, volume 121% of average), matching the screen. Snapshot mode can't draft orders or rule changes. Other models can be added with `SWINGDESK_SNAPSHOT_MODELS`, and any server that rejects tools ("does not support tools") falls back to it automatically.
- **On a host without a GPU (such as Render's free plan):** the server can't run a model itself, but two free services work:
  - **Google's Gemini API free tier:** a key from aistudio.google.com needs no billing. Set `GEMINI_API_KEY` and `SWINGDESK_FREE_TIER=on` to offer Free · Gemini. Google's free tier has rate limits, and Google may use free-tier prompts to improve its products.
  - **A hosted open-model service with a free tier**, such as Groq: set `SWINGDESK_OSS_BASE_URL` (Groq: `https://api.groq.com/openai/v1`), `SWINGDESK_OSS_MODEL` (a model from that service's list) and `SWINGDESK_OSS_API_KEY`.

  Add these in Render → tickmark → Environment, never in the repo.

**Turning on the free GPT and Gemini plans (server operator):**
1. Set `SWINGDESK_FREE_TIER=on`.
2. Add `OPENAI_API_KEY` and/or `GEMINI_API_KEY` to the server's environment, preferably from a secret manager.
3. Optionally, pin the model and change the allowance with the settings above.

A plan is only offered when its key is present. When a user's allowance runs out, the message says when it resets and which free plans they still have left.

**Why users can't sign in with ChatGPT or Gemini subscriptions.** ChatGPT Plus/Pro, Claude Pro/Max and Google AI Pro/Gemini subscriptions can't be used by other apps:
- OpenAI's "Sign in with ChatGPT" is limited to its own tools.
- Anthropic doesn't allow it.
- Google's consumer Gemini plan doesn't cover API calls from other apps.

So "sign in" here means signing in to Tickmark, and the server's keys cover the free allowance. Two options do use the user's own AI plan:
- **Claude:** add Tickmark as a connector inside Claude (see "Using your Claude plan instead" below). Claude then does the AI work on the user's Claude plan.
- **Gemini, possible later:** Google offers OAuth for the Gemini API, but it bills the user's own Google Cloud project. Offering it means registering an OAuth app with Google and passing their verification, so it isn't built yet.

A "message" is one question, however many lookups it takes. Usage is metered per user, in `portal.db`, and shown as a bar under the model picker. When the free allowance runs out, the copilot says when it resets and suggests the alternatives. With no key of their own, people start on whichever included plan the server offers.

**Using your Claude plan instead.** Claude and ChatGPT subscriptions can't be used by other apps directly: the providers don't allow it. The supported way round it is the reverse direction:
1. In Claude, add Tickmark as a **custom connector** (this server's `/mcp` URL).
2. Sign in to Tickmark and click **Allow**.
3. From then on, Claude uses Tickmark's tools, and the AI usage counts against your Claude plan.

This uses MCP's OAuth sign-in:
- discovery (`/.well-known/oauth-protected-resource` and `/oauth-authorization-server`)
- dynamic client registration
- PKCE (S256) and single-use codes
- 1-hour access tokens and rotating 30-day refresh tokens

Connected apps are listed in the Account tab, where you can disconnect them. The claude.ai web app needs a public HTTPS address for the server; Claude Desktop and Claude Code can use `localhost`.

## Connections page (sign in to brokers, exchanges and AI)

**Connections** (the plug icon in the rail or top bar, or ⌘K → "connect") shows every broker, exchange, wallet and AI option as a tile with its state. Clicking a tile opens that platform's own connect window:

1. **Sign in**, using whatever the platform supports:
   - **Sign in with &lt;broker&gt;** (OAuth): Alpaca, Schwab, Tradier and TradeStation. The server operator registers an app with each broker; until then it says "Not offered on this server yet".
   - **API keys**, checked with a read-only call before they're saved: Alpaca paper and live, Coinbase, Binance and Binance.US, Kraken, OKX, Bybit, Robinhood Crypto.
   - **Gateway:** IBKR and moomoo/Futu. **Public address:** Phantom.
   - **AI:** copilot plans, your own Claude/OpenAI/Gemini key, a local model, connectors and tokens for AI apps.
2. **Trade real money**, in the same window once connected: for Alpaca, paste live keys, then type the confirmation phrase; for crypto exchanges, pick the non-testnet account, pass its checks and type the phrase. After that the account can trade live right away; every live order still asks for its ticker.

Keys are stored encrypted for your account only and never sent back to the browser. Escape or a click outside closes the window; focus moves into the window and returns when it closes.

**TradingView** (first tile). TradingView has no sign-in API (OAuth) for other apps and doesn't allow its site inside other pages, and Tickmark won't ask for your TradingView password or session cookie. So:
- **Sign in to TradingView** opens TradingView's own sign-in page in a window beside the desk; you sign in there (Tickmark never sees the password).
- **Open my chart** (and **My TradingView** in the chart toolbar) opens your own TradingView in that same window on the symbol and candle size you're viewing, with your indicators and drawings. Paste a chart layout link (`https://www.tradingview.com/chart/<id>/`) to always open that layout; it's saved with your account.
- **Signals** come in through an alert webhook:
1. **Create webhook URL** in the TradingView window. It's shown once; only its hash is stored. A new URL replaces the old one; Disconnect turns it off.
2. In TradingView: **Alerts → Create alert → Notifications → Webhook URL**, paste it, and use the message template shown (`{"symbol": "{{ticker}}", "action": "buy", "price": {{close}}, ...}`). Plain text such as `BUY NVDA` works too. Webhooks need a paid TradingView plan.
3. Each alert appears in the alerts bell and the window's **Signals** list with **Open ticket**. A signal never places an order. Limits: 8 KB per alert, 30 alerts a minute.

## Interface

- **Command palette:** ⌘K / Ctrl K, via `cmdk`. Jump to any symbol, tab or action.
- **Icons:** `lucide-react`.
- **Toasts:** `sonner`. Errors stay up longer.
- **Tabs:** each tab has an icon. Side-panel tabs collapse to icons on narrow screens.
- **Browser details:** themed text selection, focus rings, scrollbars and caret; tabular numbers.
- **Colored stripes removed:** the thick colored stripes on cards are now tints or 1px borders.
- **Narrow windows:** layout no longer scrolls sideways.
- **Reduced motion:** honored.
- **Motion:** tab and panel changes fade and slide in about 150 ms (Framer Motion, `motion` package). Off with the system's reduced-motion setting and in focus mode.
- **Cursor:** an accent-colored pointer, a ring over buttons and links, and a crosshair over the chart. Switch it off in the brain menu in the top bar.
- **Focus mode (ADHD-friendly):** brain menu in the top bar. Bigger text, the secondary bars hidden, inactive tabs dimmed, no animation, and one **Next step** at a time with a button that takes you there: save your rules, the kill switch, review a qualifying symbol, or "nothing qualifies". It also has a 25-minute focus timer.
- **Background music:** in the same menu. Soft brown-noise rain and a slow chord pad, generated in the browser with Web Audio, so nothing is downloaded. Volume is remembered per browser.
- **Look:** a light warm-stone theme by default, after a calm portfolio-dashboard reference: white blocks on a stone canvas, a dark icon rail for the sections, sage green for up and burnt orange for down, Hanken Grotesk for text and IBM Plex Mono for numbers. The account is the one dark block, at the top of the left column: equity, change since start, cash, buying power, open positions. A dark theme with the same palette is one click away (moon icon at the bottom of the rail, or the brain menu). The chart redraws in the new colors.
- **Installable app (PWA):** open the site in Chrome or Edge and click the install icon in the top bar (or the browser's install button). On iPhone/iPad use Share → Add to Home Screen; in Safari on a Mac, File → Add to Dock. It opens in its own window with the Tickmark icon. On phones the rail becomes a bottom tab bar and the blocks stack. The service worker caches only the app shell (page, scripts, styles, icons); market data, orders and accounts always come from the server and are never stored on the device.
- **Portfolio (home):** total balance (trading account plus linked accounts' last snapshot), cash, buying power, unrealized and realized P&L, win rate, a graph of total value over time (sampled at most every 15 minutes while the desk is in use; nothing back-filled), allocation bars, and positions with average cost and P&L.
- **Charts and TradingView:** a toolbar above the chart picks the source (**Rules**: Tickmark's chart with rule markers and the planned stop; **TradingView**: TradingView's chart and indicators), the candle size (5m, 15m, 1h, 1D, 1W, 1M) and the range (1M, 3M, 6M, 1Y, 2Y). Weekly and monthly candles are built from the daily bars; intraday candles switch to TradingView. A ticker tape of SPY, QQQ and the watchlist runs under the top bar (hidden in focus mode). These are TradingView's official free widgets: their US stock prices come from Cboe One, 15 minutes delayed, and are for viewing; the rules keep using completed daily bars. TradingView no longer serves index symbols such as `FOREXCOM:SPXUSD` to free widgets, so the tape uses the SPY and QQQ ETFs.
- **Appearance:** the brain menu in the top bar. Light/dark mode is **System** by default (it follows the computer, and switches live when the computer does), or Light or Dark. Color themes: Tickmark Stone, GitHub, One Dark / One Light, Dracula / Alucard, Nord, Solarized and Monokai, each with a light and a dark version. The saved choice is applied before the page paints, and charts redraw in the new colors without resetting anything else on the desk.
- **Motion and scrolling:** same menu. Motion Full / Subtle / Off; **Smooth scrolling** (Lenis inertia on mouse wheels and trackpads; phones keep native scrolling); **Scroll effects** (cards ease in as they scroll into view, using the browser's scroll-driven animations). All off in focus mode and with the system's reduced-motion setting.
- **Installed app updates:** the service worker always loads the server's current page and keeps a copy only for offline use, so a new deploy never leaves an installed app on a stale page.
- **Connections:** every broker, exchange and AI is a tile that opens its own connect window: its sign-in methods (OAuth, API keys, gateway), the key check, and then **Trade real money** in the same window. Real money still needs the account's confirmation phrase typed once, and every live order still asks for its ticker.
- **AI strategy research:** top of the Strategy tab. Describe a goal; the copilot model reads the strategy library, your watchlist, indicators and the forward test, explains its choice and drafts a rule set. "Use these rules" saves it as a new version that screens the live watchlist. Checked 2026-11-23 with Gemma 4 E2B: it drafted an EMA-pullback rule set in about 90 seconds. Snapshot-mode models can't draft.
- **Smoothness:** contained scrolling in each panel, press feedback on buttons and tiles, spring-animated popups, no tap delay on phones.
- **Watchlist prices:** in live mode the right-hand figure is the latest trade (Alpaca IEX feed), refreshed every minute, with the change against the previous close.

## P&L stats (Journal tab)

The Journal now holds **crypto trades** next to stock trades. A crypto entry records the thesis, venue and environment, entry and exit from the exchange's own fills, fees, realized P&L, and whether the protective stop was placed.

The **P&L** panel at the top shows, for all trades, swing stocks, day-trade stocks, crypto, and each venue:
- closed and open trades, and realized P&L
- win rate, average win and loss, and profit factor
- how often the plan was followed

Crypto quoted in USDT or USDC counts as dollars. Other quote currencies are listed but left out of the totals.

## Multi-user portal (hosted)

Run with `SWINGDESK_PORTAL=on` to serve Tickmark to several people:
- **Sign-in:** email and password, hashed with scrypt. Sessions are 14-day HttpOnly, SameSite=Lax cookies.
- **CSRF protection:** every change must carry the `X-SwingDesk` header.
- **Lockout:** sign-in locks for 15 minutes after 5 failures.
- **Guests:** visitors land on the desk as a guest, like any website, with no sign-up wall. Each browser gets its own desk on real prices (Live test, Yahoo data, paper orders on the local simulator); paper trades, watchlist and journal are kept for 30 days. Guests can't connect brokers, exchanges or AI, use the copilot, or trade real money: those actions open "Create free account". Signing up from a guest desk turns it into the account, keeping everything. Unused guest desks are removed at startup after 30 days.
- **Remember me:** ticked by default on sign-in and sign-up. Ticked: the browser stays signed in for 30 days. Unticked: the cookie ends when the browser closes and the server forgets the session after a day. Accounts live in `portal.db` (users with scrypt password hashes, sessions stored as hashes only).
- **Sign-up never needs the server's setup code.** On a server with `SWINGDESK_SETUP_CODE` set (the Render blueprint generates one), everyone signs up as a normal user, and the owner becomes administrator once from **Account → Become administrator** with that code (only while no administrator exists; wrong codes are throttled). Without a setup code (the app on your own computer), the first account is the administrator.
- **Sign in with GitHub:** "Continue with GitHub" on sign-in and sign-up, and **Account → Connected sign-ins → Link GitHub** for an existing account. A guest who uses it keeps their desk; an account with the same verified email is linked rather than duplicated. Setup for the server owner: on GitHub, **Settings → Developer settings → OAuth Apps → New OAuth App**, homepage `https://tickmark-yqj2.onrender.com`, callback `https://tickmark-yqj2.onrender.com/api/auth/github/callback`; then put the client ID and a new client secret in Render → Environment as `SWINGDESK_GITHUB_CLIENT_ID` and `SWINGDESK_GITHUB_CLIENT_SECRET`. It only reads your GitHub identity and email (scopes `read:user user:email`).
- **TradingView** in **Account → Connected sign-ins**: Sign in opens TradingView's own sign-in page (see Connections for charts and alerts).
- **Sign-up (invites):** with `SWINGDESK_OPEN_SIGNUP=off`, people after the first need an invite code from the Account tab. After that, sign-up is open to anyone when `SWINGDESK_OPEN_SIGNUP=on` (the Render blueprint's default); set it to `off` to require an invite code from the Account tab.
- **Separate data per user:** each user gets their own folder (`data/users/<id>/`) holding their databases, settings, and an encrypted secret store (`secrets.enc`, Fernet). One user's broker and exchange keys, orders, journal, copilot key and history are never visible to another.
- **Operator keys stay out:** the server's own environment-variable keys (Alpaca, Anthropic) are never used for portal users.
- **Background jobs** run once per user: the daily refresh, planned exits, the day monitor, and crypto and broker sync. There's no order websocket in the portal; it polls every 30 s instead.

**Before hosting it for clients:**
- set `SWINGDESK_MASTER_KEY` from a secret manager or KMS (without it, a key file is generated next to the data, which is only fit for testing)
- serve it over HTTPS (cookies become Secure)
- back up `data/`
- if you place trades or give advice for other people, check whether you need to register (for example as an investment adviser or broker-dealer), and what custody and record-keeping rules apply

**Checked:** 6 tests cover sign-up, invites, isolation between users, encrypted secrets, operator keys never shared, CSRF, the lockout, and MCP per user. In the browser, against a separate portal-mode server with throwaway data: the first-user sign-up, the desk loading from that user's folder, invites, and sign-out.

## Connect AI apps (MCP)

Tickmark is an **MCP server** at `POST /mcp` (Streamable HTTP, JSON responses). In the **AI apps** tab (called **Account** in the portal), create a personal token, then for example:

```bash
claude mcp add --transport http tickmark http://localhost:8000/mcp --header "Authorization: Bearer sdk_..."
```

Connected apps get the copilot's 17 tools:
- `get_*` tools read the desk.
- `propose_*` tools create proposals that wait in the Copilot tab under "Waiting for you".

No tool can place, cancel or close an order, override a risk block, or change keys or settings. Tokens are stored hashed, can be revoked, and show when they were last used. The Origin header is checked.

In the portal, each user's tokens reach only that user's desk. Users also pick their own copilot model (Claude or a local model) and add their own key, which is stored encrypted in their folder.

**Checked:** against the real local server, with the **official MCP Python client (mcp 2.3.0)**: initialize (protocol 2025-06-18), tools/list (17 tools) and a tool call all worked. The test token was revoked afterwards.

## Hosting on Render (public, open sign-up)

Live since 2026-11-23 at **https://tickmark-yqj2.onrender.com** (blueprint `tickmark`, service on the free plan). From the internet: TLS is valid, `/healthz` answers, signed-out API calls get 401, and a sign-up without the setup code is refused.

The repo includes a `Dockerfile` (it builds the web app, then serves the app and the API from one slim Python image) and a `render.yaml` blueprint:

1. In Render, go to **New → Blueprint** and pick this repository. Render's GitHub app can read private repos once you allow it.
2. Render builds the image and starts it with:
   - the multi-user portal on
   - open sign-up (`SWINGDESK_OPEN_SIGNUP=on`; set `off` for invite-only)
   - Secure cookies
   - a generated `SWINGDESK_MASTER_KEY` (encrypts each user's keys)
   - a generated `SWINGDESK_SETUP_CODE`
3. Open the site and create your account (no code needed). Then copy `SWINGDESK_SETUP_CODE` from the service's **Environment** tab in Render and enter it in **Account → Become administrator**. Without the code, nobody can become admin, even with the public link.
4. Share the link. Anyone can create an account; invites from the **Account** tab still work if you turn open sign-up off.
5. Optional, free copilot plans: as the admin, open **Account → Free AI plans** and paste the server's keys (Claude, OpenAI, Gemini, or a hosted open-model key such as Groq's), and switch the free tier on. Keys are checked before saving, stored encrypted, and never sent back to the browser. Environment variables in the Render dashboard (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY`, `SWINGDESK_FREE_TIER=on`) still work and take priority. Never commit keys. Google AI Studio's Gemini key has a free tier, so it's the cheapest way to offer a free cloud plan.

**Keeping data on the free plan (encrypted backup):** the free plan wipes the disk on every sleep or redeploy. Tickmark can back the whole data folder up to a private GitHub repo and restore it on startup:
1. Create a **private** GitHub repo, for example `tickmark-data`.
2. Create a fine-grained personal access token with **Contents: read and write** on that one repo only.
3. In Render → tickmark → **Environment**, set `SWINGDESK_BACKUP_REPO` (`owner/tickmark-data`) and `SWINGDESK_BACKUP_TOKEN`.

The snapshot is encrypted with `SWINGDESK_MASTER_KEY` before it leaves the server, so GitHub only stores ciphertext; keep the master key unchanged or the backup can't be read. A snapshot is pushed soon after every sign-up, at most once a minute after other changes, and on shutdown. If the instance is killed without a shutdown signal, up to about a minute of changes can be lost. On startup with an empty disk, the latest snapshot is restored before the app starts. Checked with a fake GitHub in tests (snapshot, encryption, restore, refusing unsafe paths); not yet run against the live service.

**Free plan limits, before real clients rely on it:**
- The instance **sleeps after ~15 minutes idle**; the first visit wakes it in about a minute.
- Its **disk isn't persistent**, so accounts, journals and linked keys are **wiped on every redeploy or restart**.
- Turn on the encrypted backup above, or use a paid plan with a persistent disk mounted at `/app/data` (the local branch `render-disk-upgrade` has that blueprint, about $7.25/month, not deployed).

**Checked locally:** the Docker image can't be built on this Mac (Docker isn't installed), so Render's build is the first real build. The second stage was reproduced instead: a fresh Python 3.13 environment with only `requirements.txt`, run with the same command and settings. Results:
- the health check, the web app and its assets all load
- the API refuses signed-out requests
- the admin account can only be created with the setup code
- the session cookie is HttpOnly and Secure
- the first user gets their own desk
- sign-in metadata advertises the public https address

## Brokers at a glance

| Broker | Who uses it | Connects by | Paper account | Status |
|---|---|---|---|---|
| **Alpaca** | Retail and API-first | API key | yes | **Available** |
| **Interactive Brokers** | Active traders and professionals | Local gateway (Client Portal Gateway, browser login) | yes | **Available, paper only** |
| Charles Schwab | Largest US retail base (former TD Ameritrade) | OAuth sign-in (weekly re-login) | no (stays in the simulator) | Sign-in built; trading Phase 2 |
| Tradier | Retail and options traders | OAuth or token | sandbox | Sign-in built; trading Phase 2 |
| TradeStation | Active traders | OAuth sign-in | SIM | Sign-in built; trading Phase 2 |
| E*TRADE | Retail | OAuth 1.0a | sandbox | Phase 2 |
| moomoo / Futu | Retail (US and Asia) | Local gateway (OpenD) | yes | Phase 2 |
| Robinhood, Fidelity, Webull, Vanguard and others | Large retail bases without a public trading API | SnapTrade aggregator (trading where the broker allows it; Fidelity and Vanguard typically read-only) | no | Phase 2 |
| Prime and executing brokers | Hedge funds, asset managers, banks | FIX 4.2/4.4 session or an existing EMS | UAT | Phase 3 |

Every broker plugs into the same adapter contract (`app/brokers/base.py`, picked by `app/brokers/factory.py`), so rules, risk checks, the journal, reconciliation and alerts behave identically whichever one is connected. A shared **contract test suite** (`tests/test_adapter_contract.py`) runs identical tests against every adapter's fake server, and a new broker isn't accepted until it passes. **Step-by-step connection guides for each broker are in [docs/BROKERS.md](docs/BROKERS.md).** The Broker tab lists them all with their status.

## Connecting a broker (Alpaca Paper)

1. Create a free Alpaca account and open the **paper trading** dashboard. Generate an API key ID and secret.
2. Switch Tickmark to **Live data**. Broker orders are refused on simulated prices.
3. Open the **Broker** tab, paste both keys and click **Test and save**. Tickmark calls Alpaca's account endpoint first and saves only if that works. The keys go into this computer's **system keychain**, not the database, a file or the browser. The UI only ever shows a masked hint such as `PK…1234`. Live-trading URLs are refused.
4. Click **Send orders to Alpaca**. From then on:
   - **Sizing** uses the broker's real equity and buying power.
   - **Entries** go out with an attached stop (GTC, so the stop doesn't expire after one day). The preview id is sent as Alpaca's `client_order_id`, so a double submit can't create two orders, even at the broker.
   - **Fills, the stop leg and exits sync** from Alpaca on every refresh and whenever the Orders panel loads. The journal's P&L and plan adherence come from the broker's own fills.
   - **Planned exits** go out automatically at **3:40 PM ET** on the exit date as market-on-close orders. The stop is canceled first, because Alpaca holds shares for an open stop and would reject the sell. If that window is missed, a market exit goes out at the next run. **Send due exits** does the same on demand.
   - **Close now** cancels the stop, sends a market sell and records a plan deviation.
5. **Reconciliation** runs after every refresh, and on demand from the Broker tab. It compares broker positions and open orders with the journal and flags: positions not in the journal (trades made outside Tickmark), quantity mismatches, journal positions missing at the broker, **positions with no stop**, local orders not open at the broker, and broker orders Tickmark didn't place. In simulator mode it self-checks that every open position has a stop and a planned exit.

**Disconnect** deletes the keys from the keychain and switches back to the local simulator. Orders already at Alpaca stay there.

## Pre-trade risk checks

Every preview shows the checks, and they run again at submit, because positions, P&L and the kill switch may have changed in between.

| Check | Type | Default |
|---|---|---|
| Kill switch | **hard** | off; turning it on needs a reason |
| Broker account can trade | **hard** | Alpaca status ACTIVE, not blocked |
| Restricted symbols | **hard** | none |
| Enough buying power | **hard** | broker (or simulator) buying power |
| Daily loss limit | **hard** | 2% of equity (realized on the latest session plus open P&L) |
| Open positions | soft | 5 |
| Total open risk ("portfolio heat") | soft | 2.5% of equity (sum of entry minus stop, times shares) |
| Sector concentration | soft | 40% of equity (sector from Yahoo; demo uses a fixed map) |
| Pattern-day-trader count | soft | warns before a 4th day trade in 5 sessions when equity is under $25,000 |

Hard blocks can't be overridden. A soft block needs a written reason (at least 10 characters). The reason is stored in an **append-only override log** (the database rejects edits and deletes) and copied into the journal. The trade's adherence review then shows "Passed pre-trade risk checks without overrides: ✕" with the reason. Limits are **versioned** in the **Risk** tab, so the limits that applied to any past order can be reconstructed. The design follows the hard-block and soft-block model in the trading-operations plugin's pre-trade compliance guidance.

## Day trading (Day trading tab)

Intraday trading on real 5-minute bars, with the same evidence, risk checks and journal as swing trading. Day trading needs **Live data** mode.

**Data**
- **Alpaca connected:** live sessions use Alpaca's IEX real-time feed (the only real-time feed on the free plan). Replays of past sessions use the SIP consolidated tape.
- **No keys:** Yahoo's 5-minute bars, which cover about the last 60 days and are delayed during the session.
- Only completed bars count.
- **Relative volume** is today's cumulative volume divided by the average cumulative volume at the same time of day over the previous 10 sessions, from the same feed. That's why IEX's smaller volumes don't distort it.

**Live or replay.** *Live session* follows the clock and refreshes every 30 seconds while the market is open. *Replay a past day* loads a real past session and lets you step through it one bar (or 30 minutes) at a time, or press Play. Only bars up to the cursor exist, so replay can't use hindsight. That makes it a way to practise and test day-trading rules on real data any day of the week.

**Strategies** (long only, editable and versioned):

| Strategy | Signal on the latest completed 5-minute bar | Stop | Exit |
|---|---|---|---|
| **VWAP reclaim** | previous close below session VWAP, this close above it; relative volume ≥ 1.5x | 0.1% below the lower of the bar's low and VWAP | 2R target, stop, or flat at 15:55 |
| **Opening-range breakout** | first close above the 15-minute opening-range high; relative volume ≥ 1.5x | 0.1% below the range midpoint (or low) | 2R target, stop, or flat at 15:55 |

Both strategies also require:
- the bar to end inside the entry window (09:45–15:30)
- in live sessions, a spread of 10 bps or less and a quote no older than 15 seconds

Historical quotes aren't loaded, so in replay the spread and quote checks show as "not available in replay" and aren't counted.

**Paper execution** (local simulator):
- entry at the **next bar's open** plus 2 bps
- stop and target are one-cancels-the-other; if one bar touches both, the stop is assumed first
- anything still open is sold at the close of the 15:55 bar
- **Flatten** sells at the next bar's open and counts as a plan deviation

**Alpaca Paper execution.** When the Broker tab is set to Alpaca, a day trade in the **live** session goes to Alpaca Paper as a **bracket**: a market entry plus a take-profit limit and a stop. The two exits are one-cancels-the-other, and every order is a DAY order, so nothing carries overnight. The ticket shows where the order goes and why.
- The stop and take-profit legs appear as their own rows, and their fills close the journal entry with reason `target` or `stop`.
- **Flatten** cancels both legs, waits until they're canceled, then sends a market sell.
- A server-side **day monitor** runs every 30 s during market hours, whether or not the tab is open:
  - it advances the live simulator session and syncs broker fills
  - at the flat-by time (15:55) it cancels unfilled entries and sells any open broker day trade at market (journal reason `flat`)
  - if a broker day trade is still open after 16:00, it raises a critical alert (the DAY legs have expired by then)
- These stay on the simulator: replays, IBKR (no bracket support yet), and the real-money environment, where a broker day trade is blocked.
- Swing-trade planned exits (15:40 market-on-close) skip day trades.

Duplicate submits are blocked, and a preview goes stale as soon as a new bar completes.

**PDT rule.** Every intraday trade is a day trade. With equity under $25,000 and 3 day trades in the last 5 sessions, a new day trade is a **hard block**; for swing trades it stays a soft warning.

**Checked on real data:** stepping through the real 2026-11-20 session (Alpaca SIP) found one VWAP reclaim, SNDK at 09:50 at 5.5x relative volume, plus three near misses on light volume. The ticket previewed 143 shares, stop $85.79, target $91.01, all risk checks passing. No order was placed.

**Not yet:** IBKR day-trade brackets, historical quotes for replay, short selling, and running a day trade against the real Alpaca paper API. The bracket lifecycle is tested only against the fake Alpaca server.

## Strategy library

Pick a strategy in the **Strategy** tab, or just describe it: the wording is matched to a strategy, and every vague phrase gets a written definition and an editable number.

| Strategy | Signal on the completed daily bar | Stop | Default hold |
|---|---|---|---|
| **EMA pullback** | low touches the 9-day EMA, close back above it, volume < 80% of the 20-day average | 1% below the lower of the low and the EMA | 5 sessions |
| **Breakout** | close above the prior 20-session high, volume ≥ 1.5x average, close in the top half of the day's range | 1% below the lower of the low and the breakout level | 10 |
| **Mean reversion** | close above the 200-day SMA (long-term uptrend) and 2-day RSI (Wilder) below 10 | 3% below the signal low | 5 |
| **Trend following** | 20-day EMA crosses above a rising 50-day SMA (on the day of the cross only) | 2 × ATR(14) below the close | 20 |
| **Post-earnings drift** | earnings reported just before (after the prior close, or before the open), gap up ≥ 3%, close ≥ open, volume ≥ 2x average | 1% below the gap day's low | 10 |

Every strategy shares the same machinery:
- the data and freshness rule
- the earnings-in-window rule (warn or exclude)
- the status logic (Qualifies, Near miss, Missing info, No setup)
- sizing, risk checks, the journal, the live forward test, the historical check and alerts

Each strategy brings its own Beginner wording, its own click-through facts in Analyst view, and its own chart lines (EMA; 20-day high; 200-day SMA; EMA/SMA pair). Saving a strategy creates a new rule version, so its forward-test record starts fresh.

**Historical check on the real two-year Alpaca SIP history** for NVDA, SNDK, COIN, IREN and AAPL, through 2026-11-20, using each strategy's defaults:

| Strategy | Trades | Median trade | Win rate | Strategy total | Max DD | Buy & hold, same window |
|---|---|---|---|---|---|---|
| EMA pullback | 94 | −1.71% | 37% | +11.9% | −11.8% | +227.5% |
| Breakout | 33 | −1.45% | 46% | +34.6% | −6.5% | +227.5% |
| Mean reversion | 69 | +0.88% | 52% | +51.5% | −7.5% | +10.7% * |
| Trend following | 6 | +9.60% | 67% | +22.9% | −4.1% | +379.2% * |
| Post-earnings drift | 5 | −2.99% | 20% | −0.4% | −2.1% | +227.5% |

This is in-sample and uses hindsight on five hand-picked tickers. Trend following and post-earnings drift have far too few trades to mean anything. *Mean reversion and trend following need longer warm-up history (200 and 55 sessions), so their buy-and-hold windows start later and aren't directly comparable with the other rows. Use the live forward test to judge a strategy.

## Market data: Yahoo (free) or Alpaca SIP (licensed)

In Live data mode, pick the source from the **LIVE** indicator in the top bar:

| Source | What it is | Notes |
|---|---|---|
| **Yahoo Finance** | Free, through the unofficial `yfinance` client | Research use only. Occasional gaps; for example SNDK's 11-20 bar arrived without a close. |
| **Alpaca SIP** | Licensed. The consolidated tape (all US exchanges) from Alpaca's Market Data API, split-adjusted. | Uses your Alpaca keys. The free Alpaca plan includes SIP history for completed sessions. |

Why SIP and not IEX: IEX-only volume is about 3% of the real total (NVDA on 11-20: 17.7M vs 634M shares), which would wreck the "lighter volume" rule. If Alpaca ever refuses SIP, Tickmark falls back to Yahoo and says why.

Earnings dates and sectors still come from Yahoo for both sources, because Alpaca has no earnings calendar. The data label says so.

**Stock splits** are looked up on every refresh (Alpaca corporate actions when connected, otherwise Yahoo's split history). When a split takes effect for a stock you hold or have a working order in, Tickmark:
- rescales the local fills, the order quantities and prices, the stop and any forward-test levels, so they match the split-adjusted prices and the broker's adjusted position
- applies each split once and records it
- raises an alert
- leaves the journal's entry snapshot untouched

**Data quality** is checked after every refresh: missing sessions, impossible bars (high below low, close outside the range), missing prices, zero volume, and jumps over 30% with no split on record. An issue on the latest bar makes the screener's data rule *unknown*, so a bad print can't create a signal. It also shows as **⚠ data** on the watchlist row and raises an alert.

## Real-time order updates

When orders go to a broker, Tickmark follows them live:
- **Alpaca:** a websocket to Alpaca's `trade_updates` stream (paper, or live behind the gate), using the protocol in Alpaca's docs: `auth`, then `listen` for `trade_updates`, with binary JSON events. Each event carries the full order, which is applied straight to the local orders, the stop leg and the journal. Fills, stop-outs and exits reach the journal, alerts and the screen within seconds.
- **Reconnects** use exponential backoff, and every reconnect starts with a full sync, so nothing missed while offline is lost. An event for an order Tickmark doesn't know triggers a full sync too.
- **Interactive Brokers:** polled every 10 seconds during market hours and every 5 minutes otherwise. IBKR's websocket order topic needs gateway session cookies and is a later step.
- **The screen updates itself.** The UI listens on `/api/stream` (Server-Sent Events), so pushed fills, scheduled jobs and changes made in another window or from the command line refresh it without a reload. The top bar shows **Live order updates** (green) or **reconnecting** (amber) whenever a broker is active.

Polling still runs at refreshes and before reconciliation, as the safety net.

## Alerts

The **bell** in the top bar holds the in-app feed. It records new setups, fills, closed trades (stops are flagged), failed data fetches, reconciliation breaks, the kill switch, planned exits that couldn't be sent, and scheduler failures. Each event is announced once.

Under **Delivery** you can also turn on:
- **Desktop** notifications (macOS Notification Center)
- **Slack**, through an Incoming Webhook URL saved in the keychain
- **Email** over SMTP with STARTTLS, with the password saved in the keychain

Choose the minimum severity these channels receive (default: warnings and critical). Every channel has a **Send test** button. Delivery happens after the database transaction commits, so a slow mail server never blocks trading.

## Real money: per-account live switch

Every account starts paper or read-only. Real money is switched on **per account**, by you:

| Account | How to switch it on | Then |
|---|---|---|
| Alpaca live | Broker → Real-money trading → Alpaca live: add the separate live keys (tested read-only), type the phrase | real trades go to their own journal (`swingdesk-real-money.db`); every order needs the ticker typed |
| Interactive Brokers live | same panel → IBKR live: type the phrase, then pick your live (non-DU) account at the gateway | the adapter accepts a live account only while this switch is on |
| OKX, Coinbase, Kraken, Robinhood Crypto | Broker → Linked accounts → the account → type the phrase | the Crypto tab can send real orders to it; every order needs the coin typed; the daily loss limit applies to that account |

**What the switch needs:**
- Live data mode
- risk limits deliberately saved (version 1 or later)
- the kill switch off
- the exact phrase "I understand this trades real money"

The paper track record (closed trades, plan adherence, last reconciliation) is **shown but no longer required**. Turning a switch off, or switching to Demo data, takes one click.

Use trade-only API keys. Never grant withdrawal permission.

**Not possible:** Fidelity has no public trading API, and Robinhood stocks and options have no public API (unofficial ones break Robinhood's terms). Tickmark can't place orders there.

**This path has only been tested against the fake broker server.**

## Live forward test (replaces the backtest as the main check)

The **Live forward test** tab records each qualifying setup **when the screener first sees it** on the latest completed bar, freezing its evidence. It then follows the setup on real incoming bars with the paper plan's rules: entry at the next open, the stop, and the exit on the last planned session. Nothing is back-filled: if the app wasn't refreshed on a day, that day's setups were never seen and don't count. Results are reported per rule version (a new version starts its own record) against equal-weight buy-and-hold over the same dates, with a warning until at least 20 signals have closed.

The historical replay is still there for context, behind **Show historical check**, and is labelled as using hindsight.

## The demo data

Seeded, reproducible random walks for **NVDA, SNDK, COIN, IREN** (seed `20261120`), with daily bars through **2026-11-20**. The last few sessions are shaped so the watchlist shows four teaching cases under the default rules:

| Ticker | Case | Why |
|---|---|---|
| NVDA | **Qualifies** | Low 165.76 ≤ EMA 167.16, close 168.10 ≥ EMA, volume 62% of the 20-session average, earnings 2027-02-24 (outside the window) |
| SNDK | **Near miss** | Price rules pass, but volume is 86% of average (threshold < 80%). Earnings on 2026-11-24 fall inside the holding window, so it's also flagged. |
| IREN | **Missing info** | Price and volume pass, but the next earnings date is unknown. It's labelled unknown, never assumed clear. |
| COIN | **No setup** | Closed below the EMA on 130% of average volume |

Twelve more simulated sessions exist after 2026-11-20 but stay hidden until you click **Advance one session**. Screening, previews and the replay only ever see bars up to the simulator clock.

The documents panel ships with two **fictional** earnings releases for a made-up company. Paste real ones to analyse them.

## Default rule definitions (editable, versioned)

| Phrase | Definition |
|---|---|
| "pulled back to the 9-day EMA" | On the latest **completed** daily bar, low ≤ EMA(9) and close ≥ EMA(9). EMA is seeded with a 9-close SMA, α = 2/(n+1). |
| "lighter volume" | Session volume < 80% of the mean of the **previous** 20 completed sessions (the signal session is excluded) |
| "planned exit" | Enter at the next session's open. Exit at the close of session 5 (entry day = session 1), unless the stop is hit first. |
| "earnings before my planned exit" | A scheduled report from the entry date through the exit date. You choose *warn* or *exclude*. A missing date is *unknown*. |
| (implied) stop and size | Stop = min(signal low, EMA) − 1%. Quantity = min(⌊0.5% equity ÷ (entry − stop)⌋, ⌊20% equity ÷ entry⌋). |

Status logic: all hard rules pass → **Qualifies** (with a warning if earnings fall inside the window in warn mode); no failures but something unknown → **Missing info**; exactly one hard-rule failure → **Near miss**; two or more → **No setup**.

## Simulator fill model

- Orders submitted after the close wait for the next simulated session.
- Market buys fill at the open + 5 bps. Limit buys fill at min(open, limit) if the session's low reaches the limit.
- Entry fills are capped at 2% of session volume. Any remainder stays `partially_filled`.
- The attached stop activates once any shares are filled. It fills at min(open, stop) − 5 bps, so gaps fill at the worse price.
- The planned exit sells at the exit session's close − 5 bps. A manual "close now" sells at the next open and counts as a plan deviation.
- No commissions.

## What was exercised, and what was only checked with fixtures

| Integration | Status |
|---|---|
| FastAPI + SQLite backend, all endpoints | **Exercised** by 305 pytest tests and in the browser |
| Day trades on Alpaca Paper (bracket, flatten, day monitor) | **Tested against the fake Alpaca server**: bracket body (DAY, take-profit, stop), stop and target leg rows, target fill, stop fill, the monitor's flatten at 15:55 (and that it sends only one sell), canceling an unfilled entry, the critical alert after the close, manual flatten through the broker, swing exits skipping day trades, replay staying on the simulator, and the real-money block. In the real app the monitor thread starts, and a replay ticket (SNDK, 2026-11-20 09:50) showed its route; no order was sent. **No bracket has been placed on the real Alpaca paper API yet.** That needs market hours and a qualifying signal. |
| Daily auto-refresh scheduler | **Exercised** by tests with a fake clock: next-run times including weekends and Thanksgiving, retries, skipping in demo mode or when off, and a scheduled 16:20 run that fills a paper order and advances the forward test. In the real server, the thread starts and reports its next run as Mon 2026-11-23 16:20 ET. **A real 16:20 run hasn't happened yet.** |
| One-shot refresh CLI + cron | **Exercised**: installed in the user's crontab. A one-time test entry fired at 01:06 PST under cron and fetched real Yahoo data into `data/refresh.log`, so cron can reach `~/Documents`. The test entry was then removed. |
| **Yahoo Finance live data (yfinance 1.8.0)** | **Exercised against the real service** on 2026-11-22: about two years of daily bars for NVDA, SNDK, COIN, IREN and AAPL, plus earnings dates. Through the 2026-11-20 close, none of them qualified under the default rules, so the live forward test is empty until a setup appears. Partial-bar filtering, failed fetches, provider gaps and incremental paper fills are tested with a fake yfinance fed the reproducible bars. |
| Live forward test | **Exercised** in tests and in demo mode in the browser (NVDA recorded 11-20, closed +5.36%; COIN picked up on 11-27). The live record is empty so far because nothing qualified. |
| React UI, full workflow (confirm rules → preview → confirm → simulated fill → planned exit → journal) | **Exercised** in the browser at 1440×900. A double-click on *Confirm paper order* created exactly one order. Live mode switch, refresh, adding a ticker and the forward-test tab were also exercised in the browser. |
| Local paper simulator (pending, partially filled, filled, canceled, rejected, stop, planned exit, manual exit) | **Exercised** by tests. The fill, planned exit and journal were also checked in the UI. |
| Duplicate-order protection | **Exercised**: same preview twice → 409; 5 concurrent submits → exactly 1 order; second open entry in the same symbol → blocked |
| Alpaca Paper connection and broker lifecycle | **Tested against a stateful fake Alpaca server only** (`backend/tests/fake_alpaca.py`). It covers connect and key testing, keychain storage (an in-memory stand-in in tests), entry with a GTC stop leg, fill and stop-leg sync, market-on-close planned exits, late exits, stop-outs, manual close, duplicate protection, reconciliation breaks and broker-equity sizing. **Against the real Alpaca paper API (2026-11-22):** the keys were entered by the user, tested and saved to the keychain; the account endpoint read status ACTIVE, $100,000 equity and $200,000 buying power; the positions and open-orders endpoints were read; and a reconciliation ran clean on the empty account. **Order placement, fills, the stop leg and the exits have not yet run against the real API.** That needs a qualifying setup and market hours. |
| Alerts | Feed, de-duplication, severity filtering, Slack (mock HTTP), email (fake SMTP) and desktop (command checked; no real notification sent) are **tested**. The bell and delivery settings were **checked in the browser**. No real Slack message or email was sent. |
| Paper-to-live gate | **Tested against the fake broker server**: locked when not eligible, live keys refused until eligible, the exact phrase required, orders sent to the live host only once enabled, the ticker required per order, a separate real-money journal, and switching to Demo turns it off. **Never run against a live account.** |
| Broker catalogue | Endpoint tested. The Broker tab lists all brokers with their connection method and phase. |
| Licensed market data (Alpaca SIP) | **Exercised against the real Alpaca Market Data API on 2026-11-22** with the user's paper keys (read-only): two years of SIP split-adjusted daily bars for the watchlist, with pagination; corporate actions (NVDA's 10-for-1 split from 2024); and the UI switch to Alpaca SIP, which filled SNDK's missing Yahoo close. Split adjustment of open positions, the SIP-refused fallback and the quality checks are **tested against fakes**. The quality monitor was checked on real Yahoo data, where it flagged SNDK's missing 11-20 close. |
| Streaming order updates | **Real Alpaca paper stream exercised on 2026-11-22**: Tickmark authenticated, `trade_updates` was acknowledged, the catch-up sync ran and the UI showed "Live order updates". No order events arrived (no orders were placed). Event handling (fill, stop-leg fill, unknown order, reconnect and catch-up, bad keys) is **tested against a local websocket server** that speaks the documented protocol. In the browser, the UI refreshed itself after a change made from the command line. IBKR polling is tested with stand-ins. |
| Interactive Brokers adapter | **Tested against a fake gateway** (contract suite plus a full lifecycle through the app: reply confirmation, bracket stop, fill sync, exit at 3:40, reconciliation). Refuses non-paper accounts. In the browser, the panel's error handling was checked against this Mac's real port 5000 (AirPlay), which gave the hint in about 4 seconds. **Never run against a real IBKR gateway.** |
| OAuth sign-in framework | **Tested against a fake token server**: state checks, PKCE, the code exchange, refresh, single-use state and the re-login alert. Not connected to a real Schwab, Tradier or TradeStation account (they need registered app credentials). |
| Pre-trade risk engine | **Exercised** in tests and in the browser: a soft block on the ticket was overridden with a reason, logged, and shown in the Risk tab and the journal |
| Copilot plans, MCP OAuth, Connections | **Tested**: the free allowance per user and its 429 at the limit, the open-source plan, plans off by default, your own key unmetered; the full OAuth flow (discovery, registration rules, consent, deny, PKCE failure burning the code, single-use codes, refresh rotation, disconnect, sign-in required in the portal); the connections overview. In the browser at 1024 px: the new top bar, the ⌘K palette, the Connections page (expanded rows, no sideways scroll) and the plan picker. **No real Claude connector or broker OAuth sign-in has been done**: they need a public HTTPS server and app registrations. |
| Real-money live switches | **Tested against fakes**: Alpaca live keys and the phrase, IBKR live accounts allowed only behind the switch, crypto accounts (testnet accounts refused a switch, typed coin at submit, the daily loss check present, switching off works), and a Robinhood Crypto adapter whose signatures are checked by a fake server. **No real-money order has been placed anywhere.** |
| Crypto order routing | **Tested against a fake ccxt exchange that keeps state** (8 tests): precision and minimums, every hard block, real accounts refused, confirm, duplicate client ids, fill sync, the stop placed after the fill and only once, the alert when a stop fails, close (stop canceled before the sell), cancel, the kill switch at submit, the live-data requirement, and a copilot proposal that opens the tab without sending. In the browser: the empty state against the real server, and the form and ticket with stubbed data. **No order has been sent to a real exchange or demo account.** That needs your OKX demo key. Whether OKX's stop and trigger orders behave exactly like the stand-in is unverified. |
| Copilot (Claude tool loop, proposals) | **Tested with a scripted fake Claude client** (18 tests in all, including the model picker, the local loop against a fake OpenAI-compatible server, malformed tool arguments, and switching model families): request shape (model, adaptive thinking, caching, fallbacks), thinking blocks passed back unchanged, all tool results in one message, read tools on demo data, every proposal kind, apply and dismiss, refusing stale proposals, bad inputs returned as tool errors, refusals, the step limit, and keychain-only key storage. In the browser, the setup screen was checked against the real server, and the chat layout with stubbed replies. **Never run against the real Anthropic API**, because no key is set up yet. **The local-model path was run for real** with Ollama `qwen3.5:4b`, read-only questions only, through both the API and the UI. |
| Claude explanation narrator | **Not exercised against the API.** The number guard is unit-tested. With the env var unset (the default), the deterministic templates are used. |
| Document comparison | **Exercised** on the bundled fictional samples (tests and UI). It's a deterministic keyword-and-value extractor, not a language model. |

## Audit fixes (2026-11-23)

Two audits (backend and frontend) found these; each fix has a regression test where it can be tested.
- **Backup:** a failed restore (GitHub outage, a different master key) no longer lets the next push overwrite the good backup; backup pauses without a fixed `SWINGDESK_MASTER_KEY`.
- **Server-side requests:** in portal mode the copilot's "your own local model" is off (it would run on the server, so a user-supplied URL could reach internal hosts).
- **Guests:** can't start broker or AI-app sign-ins (OAuth start, consent page, MCP), list server models or spend the server's AI key on explanations; guest creation is rate-limited per address; an active guest's desk is kept 30 days from the last visit; old guest desks are cleaned up while running.
- **Free plans:** each message is reserved atomically before the model call and refunded if it fails, so parallel requests can't overrun the allowance.
- **Sign-up** signs you in directly (a throttled address no longer leaves a created account and an error).
- **Data:** the blank-close fill is used only for the just-finished session and only if the price fits that day's range; quotes fall back to Yahoo for any symbol Alpaca didn't return; "change" compares with the previous session once today's bar is in.
- **Copilot snapshot mode:** connection errors come back as clear messages; no junk lookups when no market data is loaded.
- **Frontend:** charts use the current theme's colors (including the IDE themes' `color-mix()` tokens); changing the theme no longer wipes a half-filled ticket; refreshes keep your chart zoom; the watchlist's hidden remove button can't be tapped by accident on phones (it shows beside the price and asks first); the connect window updates after real money is switched on and sends default field values; expired sessions show the sign-in page instead of a new guest desk; empty value history and the chart tooltip render correctly; the alerts drawer fits phones; the no-data overlay no longer covers the chart toolbar; popups manage keyboard focus; the focus timer asks for notification permission; the install button survives sign-in; the service worker prunes old files.

## Review fixes (2026-11-22)

An independent code review found 12 issues. These are fixed, each with a regression test in `backend/tests/test_review_fixes.py`:

- A planned exit no longer gets lost when the exit day's bar is missing. It exits at the next usable open instead.
- A missing price from the data provider no longer crashes the simulator or stalls every later live refresh. The order keeps working, and the account uses the last known close.
- No fills at an open that had already happened. An order or forward-test signal first seen during market hours fills at the *next* open, and the preview shows the exact fill session and the shifted holding window.
- Alpaca is blocked on simulated data. Switching to Demo data forces the simulator. Planned exits never cancel broker orders locally. Broker calls run outside database locks. The protective stop is sent GTC so it doesn't expire after day one. One failed status lookup no longer hides the other orders.
- A preview made under an older rule version, or a different broker mode, is rejected at submit.
- A second manual close after cancelling the first no longer crashes.
- Earnings on the signal day are flagged, since an after-close report gaps the entry.
- A ticker can't be removed from the watchlist while it has a working order or open position.
- The trading calendar is computed from NYSE holiday rules for any year (it previously stopped at 2027 and missed 2025-01-09).
- The UI shows which panel failed instead of silently keeping stale data. The order ticket resets when rules or broker mode change, and an edited limit price can't be submitted with an old preview.

## Known limitations

- In demo mode the prices are synthetic, so a replay result says nothing about real markets. On this dataset the rules **underperform** buy-and-hold (−3.2% vs +32.0% over the same dates, 48 trades), and the app says so plainly.
- Live data comes from an unofficial Yahoo client. It can be delayed, revised or rate-limited, and Yahoo's terms limit it to personal and research use. A production version should use a licensed feed. The provider sits behind one module (`app/live_data.py`), so it can be swapped.
- The built-in scheduler runs only while the server is running. The cron job covers days when it's off, but only while the Mac is awake: cron doesn't run jobs it missed during sleep. The server's startup catch-up fills that gap the next time the app starts.
- The trading calendar is a hard-coded NYSE holiday list for 2025–2027. Half days are treated as full sessions.
- Earnings timing (before the open vs after the close) isn't modelled. Historical earnings dates are assumed to have been known at signal time.
- The document extractor recognises only eight metric types and English phrasing like "revenue of $X billion". Anything else is reported as not analysed.
- If a ticker's live fetch fails on a day and its bar arrives later, that day isn't re-simulated. A working entry fills on the next usable day, and an overdue planned exit runs at the next open, but a stop that would have triggered on the missed day isn't replayed.
- Split handling covers open positions, working orders and forward signals. Closed trades keep their original numbers, so their prices no longer match the split-adjusted chart history.
- Alpaca's daily bars may include extended-hours trades, while Yahoo's are regular-session only. Volumes and highs/lows can differ slightly between the two sources.
- The cron job reads the Alpaca keys from the keychain. If macOS blocks keychain access in the background, that refresh falls back to Yahoo and records why.
- IBKR order status is polled (every 10 s in market hours), not streamed. Alpaca's is streamed while Tickmark is running; while it's closed, nothing is followed until the next start, when the catch-up sync runs.
- If Tickmark isn't running at 3:40 PM ET on an exit date, the planned exit goes out as a market order at the next run, so it fills at a later price than the planned close. The cron job refreshes data but doesn't send broker exits.
- Partial broker fills on exits are handled when they finish. A sell that stays partially filled at the end of the day is left for the next sync. P&L and plan adherence are computed for simulator trades.

## More

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): diagram and module map
- [docs/DEMO_SCRIPT.md](docs/DEMO_SCRIPT.md): two-minute walkthrough
- [docs/EXPANSION.md](docs/EXPANSION.md): how the same components support day traders, HF/IB/PE/VC analysts
- [docs/ROADMAP.md](docs/ROADMAP.md): what to build next to connect a real broker and serve firms
