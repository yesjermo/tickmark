"""One place that knows which adapter serves which broker id.

Broker ids are stored on every order row (orders.broker) and as the active broker
mode. "alpaca_paper" is kept as Alpaca's id for compatibility; with the live gate
enabled the same adapter points at Alpaca's live endpoint.
"""
from __future__ import annotations

from .base import BrokerAdapter, BrokerError

SIMULATOR = "simulator"
EXTERNAL = ("alpaca_paper", "ibkr")
LABELS = {"simulator": "Local simulator", "alpaca_paper": "Alpaca", "ibkr": "Interactive Brokers"}


def adapter(broker_id: str) -> BrokerAdapter:
    if broker_id == "alpaca_paper":
        from . import alpaca
        return alpaca.AlpacaPaperBroker()
    if broker_id == "ibkr":
        from . import ibkr
        return ibkr.IBKRBroker()
    raise BrokerError(f"No broker adapter for '{broker_id}'")


def connected(broker_id: str) -> bool:
    if broker_id == "alpaca_paper":
        from . import alpaca
        return alpaca.configured()
    if broker_id == "ibkr":
        from . import ibkr
        return ibkr.configured()
    return broker_id == SIMULATOR


def capabilities(broker_id: str) -> dict:
    if broker_id == "alpaca_paper":
        from . import alpaca
        return alpaca.AlpacaPaperBroker.capabilities
    if broker_id == "ibkr":
        from . import ibkr
        return ibkr.IBKRBroker.capabilities
    return {"paper": True, "attached_stop": True, "market_on_close": True, "real_money": False, "bracket": True}


def is_external(broker_id: str | None) -> bool:
    return broker_id in EXTERNAL
