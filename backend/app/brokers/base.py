from __future__ import annotations

from dataclasses import dataclass, field

# The app's order-status vocabulary. Adapters map broker-specific states onto it.
STATUSES = ("pending", "partially_filled", "filled", "canceled", "rejected")
OPEN_STATUSES = ("pending", "partially_filled")


class BrokerError(Exception):
    pass


@dataclass
class OrderRequest:
    client_order_id: str
    symbol: str
    side: str                 # buy | sell
    qty: int
    order_type: str           # market | limit
    limit_price: float | None
    stop_price: float | None  # protective stop attached to the entry (one-triggers-other)


@dataclass
class BrokerResult:
    broker_order_id: str | None
    status: str
    status_reason: str | None = None
    filled_qty: int = 0
    avg_fill_price: float | None = None
    legs: list = field(default_factory=list)


class BrokerAdapter:
    """The contract every broker implements. Tickmark's rules, risk checks, journal,
    reconciliation and alerts only ever call these methods.

    Results are normalised to BrokerResult with the app's status vocabulary; an entry's
    attached protective stop is reported in `legs` as a sell-side stop.
    """
    name = "base"
    capabilities = {
        "paper": True,            # a paper/sandbox account exists
        "attached_stop": True,    # entry can carry a child stop that activates on fill
        "market_on_close": False, # planned exits can be sent as MOC; otherwise a market order near the close
        "real_money": False,      # this adapter may be used behind the live gate
        "bracket": False,         # entry + take-profit + stop as one OCO bracket (used for day trades)
    }

    def account(self) -> dict: raise NotImplementedError          # equity, cash, buying_power, status, flags
    def positions(self) -> dict[str, dict]: raise NotImplementedError   # symbol -> {qty, avg_entry_price, ...}
    def open_orders(self) -> list[dict]: raise NotImplementedError      # [{symbol, side, result: BrokerResult}]
    def submit(self, req: "OrderRequest") -> "BrokerResult": raise NotImplementedError
    def sell(self, symbol: str, qty: int, client_order_id: str, at_close: bool = False) -> "BrokerResult": raise NotImplementedError
    def get(self, broker_order_id: str) -> "BrokerResult": raise NotImplementedError
    def get_by_client_id(self, client_order_id: str) -> "BrokerResult": raise NotImplementedError
    def cancel(self, broker_order_id: str) -> None: raise NotImplementedError
