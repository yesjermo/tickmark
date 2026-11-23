"""Catalogue of brokers and how each one connects.

Every broker plugs into the same adapter contract (submit with attached stop, sell,
get/cancel order, open orders, positions, account). What differs is how a user
connects. There are five patterns:

  api_key     paste a key pair once (Alpaca)
  oauth2      "Connect with <broker>" -> broker login page -> tokens refreshed automatically
              (Schwab, Tradier, TradeStation; E*TRADE uses OAuth 1.0a)
  gateway     a small broker program runs on your machine and the app talks to it
              (Interactive Brokers TWS / IB Gateway, moomoo OpenD)
  aggregator  one consent screen covers many retail brokers that have no public API
              (SnapTrade; Plaid Investments for read-only holdings)
  fix         institutional FIX session to an executing or prime broker, after onboarding

Alpaca and Interactive Brokers (paper) are implemented for trading. Entries with status "linked" connect
read-only through app/linked.py (balances and positions, no orders); the OAuth sign-in framework is in app/oauth.py; the rest are listed with the phase that adds them.
"""

BROKERS = [
    {"id": "alpaca", "name": "Alpaca", "segment": "Retail and API-first", "connect": "api_key", "paper": True,
     "status": "available", "phase": 1, "assets": "US stocks, ETFs, options, crypto",
     "how": "Paste the API key ID and secret from the Alpaca dashboard. Paper and live keys are separate."},
    {"id": "ibkr", "name": "Interactive Brokers", "segment": "Active traders and professionals", "connect": "gateway", "paper": True,
     "status": "available", "phase": 2, "assets": "Global stocks, options, futures, FX, bonds",
     "how": "Run IBKR's Client Portal Gateway, log in at https://localhost:5000 in your browser (IBKR handles the password and "
            "two-factor), then pick your paper account (DU...) in Tickmark. Paper accounts only in this build. "
            "A TWS / IB Gateway socket adapter (ports 7497 paper, 7496 live) is a later option."},
    {"id": "schwab", "name": "Charles Schwab", "segment": "Retail (largest US base, former TD Ameritrade)", "connect": "oauth2", "paper": False,
     "status": "planned", "phase": 2, "assets": "US stocks, ETFs, options",
     "how": "Click Connect with Schwab, log in on Schwab's site and approve access. Tokens refresh automatically; "
            "Schwab requires logging in again about every 7 days. The API has no paper trading, so paper testing stays in the simulator."},
    {"id": "tradier", "name": "Tradier", "segment": "Retail and active options traders", "connect": "oauth2", "paper": True,
     "status": "planned", "phase": 2, "assets": "US stocks, ETFs, options",
     "how": "Connect with Tradier (OAuth) or paste a personal access token. A free sandbox account covers paper testing."},
    {"id": "tradestation", "name": "TradeStation", "segment": "Active traders", "connect": "oauth2", "paper": True,
     "status": "planned", "phase": 2, "assets": "US stocks, options, futures",
     "how": "Connect with TradeStation (OAuth). Simulated (SIM) accounts are available for paper testing."},
    {"id": "etrade", "name": "E*TRADE (Morgan Stanley)", "segment": "Retail", "connect": "oauth1", "paper": True,
     "status": "planned", "phase": 2, "assets": "US stocks, ETFs, options",
     "how": "Connect with E*TRADE (OAuth 1.0a). A sandbox environment is available for testing."},
    {"id": "snaptrade", "name": "SnapTrade aggregator (Robinhood, Fidelity, Webull, Vanguard, Wealthsimple and others)",
     "segment": "Retail brokers without a public trading API", "connect": "aggregator", "paper": False,
     "status": "planned", "phase": 2, "assets": "Depends on the broker",
     "how": "One SnapTrade connection screen links the user's broker account. Holdings and orders sync, and trading is "
            "available only where that broker allows it through SnapTrade. Fidelity and Vanguard are typically read-only."},
    {"id": "moomoo", "name": "moomoo / Futu", "segment": "Retail (US and Asia)", "connect": "gateway", "paper": True,
     "status": "linked", "phase": 2, "assets": "US and Hong Kong stocks, options", "linked_provider": "futu",
     "how": "Run the OpenD gateway and log in there; Tickmark connects to it locally (paper account by default). "
            "Linked read-only today (balances and positions); order routing is next."},
    {"id": "robinhood", "name": "Robinhood", "segment": "Retail (largest app-first US base)", "connect": "api_key", "paper": False,
     "status": "linked", "phase": 2, "assets": "Crypto via the official API; stocks only through an aggregator", "linked_provider": "robinhood_crypto",
     "how": "Robinhood Crypto has an official API (Ed25519 key pair): link it, then turn on its real-money switch to trade "
            "crypto (no sandbox exists). Robinhood has no public API for stocks or options; unofficial ones break its terms "
            "and aren't used, so Robinhood stocks stay out of reach until an aggregator such as SnapTrade offers trading."},
    {"id": "webull", "name": "Webull", "segment": "Retail and active traders", "connect": "api_key", "paper": True,
     "status": "planned", "phase": 2, "assets": "US stocks, ETFs, options",
     "how": "Webull's OpenAPI needs an application approved by Webull (app key and secret). Until then, link it through SnapTrade."},
    {"id": "fidelity", "name": "Fidelity", "segment": "Retail and retirement (largest US asset base)", "connect": "aggregator", "paper": False,
     "status": "planned", "phase": 2, "assets": "US stocks, ETFs, funds",
     "how": "Fidelity has no public trading API, so Tickmark can't place Fidelity orders. Holdings could be linked read-only "
            "through SnapTrade or Plaid (planned); orders stay in Fidelity's own app."},
    {"id": "coinbase", "name": "Coinbase", "segment": "Crypto exchange (largest US)", "connect": "api_key", "paper": False,
     "status": "linked", "phase": 2, "assets": "Spot crypto", "linked_provider": "coinbase",
     "how": "Advanced Trade API key (CDP). Read-only with a View key; live trading with a Trade key after you turn on the account's real-money switch. No spot sandbox."},
    {"id": "binance", "name": "Binance / Binance.US", "segment": "Crypto exchange (largest global)", "connect": "api_key", "paper": True,
     "status": "linked", "phase": 2, "assets": "Spot crypto", "linked_provider": "binance",
     "how": "Spot Testnet keys by default (testnet.binance.vision); a read-only key for the real account. US residents use Binance.US."},
    {"id": "kraken", "name": "Kraken", "segment": "Crypto exchange", "connect": "api_key", "paper": False,
     "status": "linked", "phase": 2, "assets": "Spot crypto", "linked_provider": "kraken",
     "how": "API key with Query Funds (read-only), plus Create & Modify Orders to trade after you turn on the account's real-money switch. No spot sandbox; never grant withdrawal."},
    {"id": "okx", "name": "OKX", "segment": "Crypto exchange", "connect": "api_key", "paper": True,
     "status": "linked", "phase": 2, "assets": "Spot crypto", "linked_provider": "okx",
     "how": "Demo-trading key by default (key, secret, passphrase). For the real account, a Read key, or a Trade key plus the account's real-money switch. Never grant withdrawal."},
    {"id": "bybit", "name": "Bybit", "segment": "Crypto exchange", "connect": "api_key", "paper": True,
     "status": "linked", "phase": 2, "assets": "Spot crypto", "linked_provider": "bybit",
     "how": "Testnet keys by default (testnet.bybit.com); a Read-Only key for the real account."},
    {"id": "phantom", "name": "Phantom (Solana wallet)", "segment": "Self-custody crypto wallet", "connect": "address", "paper": False,
     "status": "linked", "phase": 2, "assets": "SOL and SPL tokens", "linked_provider": "phantom",
     "how": "Watch-only: paste the public address. A wallet has no broker API; trading would need the wallet to sign each "
            "transaction, and Tickmark never asks for a seed phrase or private key."},
    {"id": "fix", "name": "Institutional FIX (prime and executing brokers)", "segment": "Hedge funds, asset managers, banks",
     "connect": "fix", "paper": True, "status": "planned", "phase": 3, "assets": "Multi-asset",
     "how": "A FIX 4.2/4.4 session (sender and target comp IDs, certification in the broker's UAT environment) through a "
            "FIX engine, or through an execution management system the firm already uses."},
]

CONNECT_LABEL = {"address": "Public address", "api_key": "API key", "oauth2": "OAuth sign-in", "oauth1": "OAuth sign-in (1.0a)", "gateway": "Local gateway",
                 "aggregator": "Aggregator", "fix": "FIX session"}


def catalogue() -> list[dict]:
    return [{**b, "connect_label": CONNECT_LABEL[b["connect"]]} for b in BROKERS]
