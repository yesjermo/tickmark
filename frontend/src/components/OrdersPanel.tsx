import { api, ApiError, Order } from "../api";
import { OrderStatus, money } from "./ui";

export function OrdersPanel({ orders, onChanged, notify }: { orders: Order[]; onChanged: () => void; notify: (s: string) => void }) {
  const entries = orders.filter((o) => o.purpose === "entry");
  async function act(path: string, msg: string) {
    try { await api.post(path); notify(msg); onChanged(); } catch (e) { notify((e as ApiError).message); }
  }
  if (!entries.length) return <p className="muted pad">No paper orders yet.</p>;
  return (
    <div className="orders">
      {entries.map((o) => {
        const sells = orders.filter((x) => x.parent_id === o.id && x.side === "sell");
        const sold = sells.reduce((a, s) => a + s.filled_qty, 0);
        const open = o.filled_qty - sold;
        return (
          <div key={o.id} className="order card">
            <div className="row between">
              <b>BUY {o.qty} {o.symbol}</b>
              <OrderStatus s={o.status} />
            </div>
            <p className="small muted">{o.order_type}{o.limit_price ? ` ${money(o.limit_price)}` : ""} · {o.broker} · <code>{o.id}</code></p>
            <p className="small">{o.status_reason}</p>
            {o.fills.map((f, i) => <p key={i} className="small num">fill {f.session_date}: {f.qty} @ {money(f.price)}</p>)}
            {sells.map((s) => (
              <p key={s.id} className="small">↳ {s.purpose.replace("_", " ")} {s.stop_price ? `@ ${money(s.stop_price)}` : ""} <OrderStatus s={s.status} /> <span className="muted">{s.status_reason}</span></p>
            ))}
            <div className="row">
              {(o.status === "pending" || o.status === "partially_filled") && (
                <button className="btn small ghost" onClick={() => act(`/api/orders/${o.id}/cancel`, "Order canceled.")}>Cancel {o.filled_qty ? "remainder" : "order"}</button>
              )}
              {open > 0 && !sells.some((s) => s.purpose === "manual_exit" && s.status === "pending") && (
                <button className="btn small ghost" onClick={() => act(`/api/orders/${o.id}/close`, "Manual exit queued for the next open (counts as a plan deviation).")}>
                  Close {open} now (off-plan)
                </button>
              )}
            </div>
          </div>
        );
      })}
    </div>
  );
}
