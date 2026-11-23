"""The broker adapter contract: every adapter must pass the same tests against its own fake.

Add a new broker by giving it a fake server and a fixture entry below; the rest of the
app (rules, risk, journal, reconciliation, alerts) relies only on these behaviours.
"""
import pytest

from app.brokers import alpaca, ibkr
from app.brokers.base import OrderRequest
from fake_alpaca import GOOD_KEY, GOOD_SECRET, FakeAlpaca
from fake_ibkr import ACCOUNT, GATEWAY, FakeIBKR


@pytest.fixture(params=["alpaca", "ibkr"])
def broker(request, client):   # `client` isolates the database the adapters may cache into
    if request.param == "alpaca":
        fake = FakeAlpaca()
        return alpaca.AlpacaPaperBroker(transport=fake.transport(), key=GOOD_KEY, secret=GOOD_SECRET), fake
    fake = FakeIBKR()
    return ibkr.IBKRBroker(transport=fake.transport(), gateway_url=GATEWAY, account_id=ACCOUNT), fake


def entry(cid="pv_c1", qty=10, stop=150.0):
    return OrderRequest(cid, "NVDA", "buy", qty, "market", None, stop)


def test_account_shape(broker):
    b, _ = broker
    a = b.account()
    assert a["equity"] > 0 and a["buying_power"] > 0 and a["status"] == "ACTIVE" and a["trading_blocked"] is False


def test_entry_with_attached_stop(broker):
    b, _ = broker
    r = b.submit(entry())
    assert r.status == "pending" and r.broker_order_id
    assert len(r.legs) == 1 and r.legs[0]["side"] == "sell" and r.legs[0]["stop_price"] == 150.0


def test_duplicate_client_order_id_returns_original(broker):
    b, fake = broker
    first = b.submit(entry())
    again = b.submit(entry())
    assert again.broker_order_id == first.broker_order_id


def test_fill_activates_stop_and_creates_position(broker):
    b, fake = broker
    r = b.submit(entry())
    fake.fill_entry("pv_c1", 160.0)
    g = b.get(r.broker_order_id)
    assert g.status == "filled" and g.filled_qty == 10 and g.avg_fill_price == 160.0
    assert g.legs and g.legs[0]["status"] == "pending"
    assert b.positions()["NVDA"]["qty"] == 10
    assert any(o["symbol"] == "NVDA" and (o["side"] == "sell" or o["result"].legs) for o in b.open_orders())


def test_stop_fill_closes_position(broker):
    b, fake = broker
    r = b.submit(entry())
    fake.fill_entry("pv_c1", 160.0)
    fake.trigger_stop("NVDA", 149.5)
    g = b.get(r.broker_order_id)
    assert g.legs[0]["status"] == "filled" and g.legs[0]["avg_fill_price"] == 149.5
    assert "NVDA" not in b.positions()


def test_exit_after_canceling_stop(broker):
    b, fake = broker
    r = b.submit(entry())
    fake.fill_entry("pv_c1", 160.0)
    leg_id = b.get(r.broker_order_id).legs[0]["id"]
    b.cancel(leg_id)
    s = b.sell("NVDA", 10, "pv_c1:exit")
    assert s.status == "pending" and s.broker_order_id
    fake.fill_sells("NVDA", 170.0)
    assert "NVDA" not in b.positions() and b.get(s.broker_order_id).status == "filled"


def test_cancel_working_entry(broker):
    b, _ = broker
    r = b.submit(entry())
    b.cancel(r.broker_order_id)
    assert b.get(r.broker_order_id).status == "canceled"


def test_selling_more_than_held_is_rejected_not_raised(broker):
    b, _ = broker
    s = b.sell("NVDA", 5, "pv_c1:oversell")
    assert s.status == "rejected"
