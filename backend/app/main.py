from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import alerts, events, market_data, oauth, streaming, backtest, broker_sync, credentials, db, demo_data, documents, explain, live_data, live_gate, risk, scheduler, trading
from .brokers import factory, ibkr, registry
from . import day as day_mod
from starlette.concurrency import run_in_threadpool
from .brokers import alpaca
from .brokers import simulator as sim
from .trading import TradeError
from . import copilot as copilot_mod
from . import linked as linked_mod
from . import crypto_trading as crypto_mod
from . import pnl as pnl_mod
from . import portal
from . import mcp_server
from . import mcp_oauth


TRADING_TABLES = ("previews", "orders", "fills", "journal_outcomes", "forward_signals")


def ensure_db(mode: str, reset: bool = False) -> None:
    conn = db.connect(mode)
    try:
        db.init_schema(conn)
        if mode == "demo":
            demo_data.seed(conn, reset=reset)
            return
        if reset:  # live: wipe paper trading state, keep cached market data and rules
            for t in TRADING_TABLES:
                conn.execute(f"DELETE FROM {t}")
            conn.execute("DROP TRIGGER IF EXISTS journal_immutable")
            conn.execute("DELETE FROM journal")
            db.init_schema(conn)
        if db.kv_get(conn, "watchlist") is None:
            db.kv_set(conn, "watchlist", list(demo_data.TICKERS))
        if reset or db.kv_get(conn, "account") is None:
            db.kv_set(conn, "account", {"starting_equity": 100000.0, "cash": 100000.0})
    finally:
        conn.close()


SCHED: scheduler.Scheduler | None = None
STREAM: streaming.OrderStream | None = None
DAYMON = None


@asynccontextmanager
async def lifespan(app):
    global SCHED, STREAM, DAYMON
    from . import day_monitor
    portal.for_each_tenant(lambda: ensure_db(db.data_mode()))
    SCHED = scheduler.Scheduler()
    SCHED.start()
    STREAM = streaming.OrderStream()
    if not portal.enabled():          # the order websocket is per account; the portal relies on the per-user polling sync instead
        STREAM.start()
    DAYMON = day_monitor.DayMonitor()
    DAYMON.start()
    yield
    SCHED.stop()
    STREAM.stop()
    DAYMON.stop()


app = FastAPI(title="Tickmark API", lifespan=lifespan)


@app.middleware("http")
async def deliver_alerts(request: Request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"   # trading state must never come from a browser cache
        if request.method in ("POST", "PUT", "DELETE") and response.status_code < 400 \
                and not request.url.path.startswith(("/api/alerts/read", "/api/strategy/parse", "/api/previews", "/api/documents", "/api/copilot/chat", "/api/crypto/preview")):
            events.bump(f"{request.method} {request.url.path}")   # other open windows refresh too
        if not request.url.path.startswith("/api/alerts"):
            await run_in_threadpool(alerts.flush)   # sends anything new to enabled channels, outside any transaction
    return response


PUBLIC_API = ("/api/auth/",)


@app.middleware("http")
async def portal_auth(request: Request, call_next):
    """Portal mode: every /api call belongs to a signed-in user and runs inside that user's own data folder."""
    path = request.url.path
    if not portal.enabled() or not path.startswith("/api/") or path.startswith(PUBLIC_API):
        return await call_next(request)
    user = await run_in_threadpool(portal.user_for_session, request.cookies.get(portal.SESSION_COOKIE))
    if not user:
        return JSONResponse(status_code=401, content={"detail": "Sign in first.", "signin": True}, headers={"Cache-Control": "no-store"})
    if request.method in ("POST", "PUT", "DELETE", "PATCH") and request.headers.get("x-swingdesk") != "1":
        return JSONResponse(status_code=403, content={"detail": "Missing X-SwingDesk header (cross-site request refused)."})
    request.state.user = user
    tok = db.TENANT.set(portal.tenant_dir(user["id"]))
    utok = portal.CURRENT_USER.set(user["id"])
    try:
        return await call_next(request)
    finally:
        db.TENANT.reset(tok)
        portal.CURRENT_USER.reset(utok)


def _me(request: Request) -> dict:
    return getattr(request.state, "user", None) or {"id": portal.LOCAL_USER, "role": "admin", "email": None}


def _cookie(resp: JSONResponse, request: Request, token: str | None) -> JSONResponse:
    secure = request.url.scheme == "https" or os.environ.get("SWINGDESK_SECURE_COOKIES", "off") == "on"
    if token:
        resp.set_cookie(portal.SESSION_COOKIE, token, max_age=portal.SESSION_DAYS * 86400, httponly=True, samesite="lax",
                        secure=secure, path="/")
    else:
        resp.delete_cookie(portal.SESSION_COOKIE, path="/")
    return resp


class SignupIn(BaseModel):
    email: str
    password: str
    name: str = ""
    invite: str = ""


class LoginIn(BaseModel):
    email: str
    password: str


class InviteIn(BaseModel):
    email: str = ""


class TokenIn(BaseModel):
    name: str = "AI app"


@app.exception_handler(portal.PortalError)
async def portal_error(_: Request, exc):
    return JSONResponse(status_code=exc.code, content={"detail": str(exc)}, headers={"Cache-Control": "no-store"})


@app.get("/api/auth/status")
def auth_status(request: Request):
    if not portal.enabled():
        return {"portal": False, "user": None}
    user = portal.user_for_session(request.cookies.get(portal.SESSION_COOKIE))
    first = not portal.user_ids()
    return {"portal": True, "user": user, "first_user": first,
            "open_signup": os.environ.get("SWINGDESK_OPEN_SIGNUP", "off").lower() in ("on", "1", "true")}


@app.post("/api/auth/signup")
def auth_signup(body: SignupIn, request: Request):
    if not portal.enabled():
        raise HTTPException(404, "The multi-user portal is off (SWINGDESK_PORTAL=on enables it).")
    u = portal.signup(body.email, body.password, body.name, body.invite)
    with portal.as_user(u["id"]):
        ensure_db("demo")
        ensure_db("live")
    token, u = portal.login(body.email, body.password, request.client.host if request.client else "")
    return _cookie(JSONResponse({"user": u}), request, token)


@app.post("/api/auth/login")
def auth_login(body: LoginIn, request: Request):
    if not portal.enabled():
        raise HTTPException(404, "The multi-user portal is off.")
    token, u = portal.login(body.email, body.password, request.client.host if request.client else "")
    return _cookie(JSONResponse({"user": u}), request, token)


@app.post("/api/auth/logout")
def auth_logout(request: Request):
    portal.logout(request.cookies.get(portal.SESSION_COOKIE))
    return _cookie(JSONResponse({"ok": True}), request, None)


@app.post("/api/portal/invites")
def portal_invite(body: InviteIn, request: Request):
    return portal.create_invite(_me(request), body.email)


@app.get("/api/portal/users")
def portal_users(request: Request):
    return portal.list_users(_me(request))


@app.get("/api/mcp/tokens")
def mcp_tokens(request: Request):
    return {"tokens": portal.list_tokens(_me(request)["id"]), "endpoint": f"{str(request.base_url).rstrip('/')}/mcp",
            "tools": [t["name"] for t in copilot_mod.TOOLS]}


@app.post("/api/mcp/tokens")
def mcp_token_create(body: TokenIn, request: Request):
    return portal.create_token(_me(request)["id"], body.name)


@app.delete("/api/mcp/tokens/{tid}")
def mcp_token_revoke(tid: str, request: Request):
    portal.revoke_token(_me(request)["id"], tid)
    return {"revoked": tid}


def _origin_ok(origin: str | None) -> bool:
    if not origin:
        return True                                   # non-browser MCP clients send no Origin
    allowed = [o.strip() for o in os.environ.get("SWINGDESK_ALLOWED_ORIGINS", "").split(",") if o.strip()]
    from urllib.parse import urlparse
    host = urlparse(origin).hostname or ""
    return origin in allowed or host in ("localhost", "127.0.0.1", "::1")


@app.post("/mcp")
async def mcp_endpoint(request: Request):
    if not _origin_ok(request.headers.get("origin")):
        return JSONResponse(status_code=403, content={"error": "origin not allowed"})
    auth = request.headers.get("authorization", "")
    bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else None
    if bearer and bearer.startswith("sdo_"):
        who = await run_in_threadpool(mcp_oauth.user_for_access, bearer)       # signed in through an AI app (OAuth)
    else:
        who = await run_in_threadpool(portal.user_for_token, bearer)            # personal access token
    if not who:
        base = mcp_oauth.base_url(str(request.base_url))
        return JSONResponse(status_code=401, content={"error": "invalid or missing token"},
                            headers={"WWW-Authenticate": f'Bearer realm="tickmark", resource_metadata="{base}/.well-known/oauth-protected-resource"'})
    try:
        body = await request.json()
    except ValueError:
        return JSONResponse(status_code=400, content={"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "Parse error"}})
    uid, tid = who

    def run():
        with portal.as_user(uid if portal.enabled() else None):
            return mcp_server.handle(body, tid)
    out = await run_in_threadpool(run)
    if out is None:
        return JSONResponse(status_code=202, content=None)
    return JSONResponse(out)


# ---------- OAuth for MCP clients ("sign in with your AI app") ----------

@app.exception_handler(mcp_oauth.OAuthError)
async def oauth_error(_: Request, exc):
    return JSONResponse(status_code=exc.code, content={"error": exc.error, "error_description": exc.desc}, headers={"Cache-Control": "no-store"})


@app.get("/.well-known/oauth-protected-resource")
@app.get("/.well-known/oauth-protected-resource/mcp")
def oauth_resource(request: Request):
    return mcp_oauth.resource_metadata(mcp_oauth.base_url(str(request.base_url)))


@app.get("/.well-known/oauth-authorization-server")
def oauth_server(request: Request):
    return mcp_oauth.server_metadata(mcp_oauth.base_url(str(request.base_url)))


@app.post("/oauth/register")
async def oauth_register(request: Request):
    try:
        body = await request.json()
    except ValueError:
        raise mcp_oauth.OAuthError("invalid_client_metadata", "JSON body required")
    return JSONResponse(status_code=201, content=mcp_oauth.register(body))


def _oauth_user(request: Request) -> dict | None:
    if not portal.enabled():
        return {"id": portal.LOCAL_USER, "email": "this computer"}
    return portal.user_for_session(request.cookies.get(portal.SESSION_COOKIE))


@app.get("/oauth/authorize")
def oauth_authorize(request: Request):
    from urllib.parse import quote
    q = dict(request.query_params)
    try:
        params = mcp_oauth.validate_authorize(q)
    except mcp_oauth.OAuthError as exc:
        return HTMLResponse(f"<p>Can't connect: {exc.desc}</p>", status_code=400)
    user = _oauth_user(request)
    if not user:          # sign in to Tickmark first, then come back here
        from fastapi.responses import RedirectResponse
        return RedirectResponse("/?next=" + quote(str(request.url.path) + "?" + str(request.url.query), safe=""), status_code=302)
    rid = mcp_oauth.start_consent(user["id"], params)
    return HTMLResponse(mcp_oauth.consent_page(rid, params, user.get("email") or "you"), headers={"Cache-Control": "no-store", "X-Frame-Options": "DENY"})


@app.post("/oauth/authorize")
async def oauth_decide(request: Request):
    from urllib.parse import parse_qs
    from fastapi.responses import RedirectResponse
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    user = _oauth_user(request)
    if not user:
        return HTMLResponse("<p>Sign in to Tickmark first.</p>", status_code=401)
    try:
        url = mcp_oauth.decide(form.get("request_id", ""), user["id"], form.get("decision") == "approve")
    except mcp_oauth.OAuthError as exc:
        return HTMLResponse(f"<p>{exc.desc}</p>", status_code=400)
    return RedirectResponse(url, status_code=302)


@app.post("/oauth/token")
async def oauth_token(request: Request):
    from urllib.parse import parse_qs
    raw = (await request.body()).decode()
    form = {k: v[0] for k, v in parse_qs(raw).items()}
    out = await run_in_threadpool(mcp_oauth.token, form)
    return JSONResponse(out, headers={"Cache-Control": "no-store", "Pragma": "no-cache"})


@app.post("/oauth/revoke")
async def oauth_revoke(request: Request):
    from urllib.parse import parse_qs
    form = {k: v[0] for k, v in parse_qs((await request.body()).decode()).items()}
    mcp_oauth.revoke(form.get("token", ""))
    return JSONResponse({})


@app.get("/api/mcp/apps")
def mcp_apps(request: Request):
    return mcp_oauth.connected_apps(_me(request)["id"])


@app.delete("/api/mcp/apps/{client_id}")
def mcp_app_disconnect(client_id: str, request: Request):
    mcp_oauth.disconnect(_me(request)["id"], client_id)
    return {"disconnected": client_id}


@app.get("/mcp")
def mcp_get():
    return JSONResponse(status_code=405, content={"error": "this server answers POST only (no SSE stream)"}, headers={"Allow": "POST"})


@app.exception_handler(day_mod.DayError)
async def day_error(_: Request, exc):
    return JSONResponse(status_code=exc.code, content={"detail": str(exc), **(exc.payload or {})})


@app.exception_handler(crypto_mod.CryptoError)
async def crypto_error(_: Request, exc):
    return JSONResponse(status_code=exc.code, content={"detail": str(exc), **(exc.payload or {})}, headers={"Cache-Control": "no-store"})


@app.exception_handler(linked_mod.LinkError)
async def linked_error(_: Request, exc):
    return JSONResponse(status_code=exc.code, content={"detail": str(exc)}, headers={"Cache-Control": "no-store"})


@app.exception_handler(copilot_mod.CopilotError)
async def copilot_error(_: Request, exc):
    return JSONResponse(status_code=exc.code, content={"detail": str(exc)}, headers={"Cache-Control": "no-store"})


@app.exception_handler(TradeError)
async def trade_error(_: Request, exc: TradeError):
    return JSONResponse(status_code=exc.code, content={"detail": str(exc), **(exc.payload or {})})


@app.exception_handler(ValueError)
async def value_error(_: Request, exc: ValueError):
    return JSONResponse(status_code=400, content={"detail": str(exc)})


def conn():
    return db.connect()


@app.get("/api/status")
def status():
    c = conn()
    try:
        live = db.data_mode() == "live"
        return {"as_of": trading.as_of(c), "mode": trading.broker_mode(c), "alpaca_configured": alpaca.configured(),
                "mode_label": factory.LABELS.get(trading.broker_mode(c), trading.broker_mode(c)),
                "ibkr_configured": ibkr.configured(),
                "llm": "claude" if os.environ.get("SWINGDESK_LLM") == "claude" else "off (deterministic explanations)",
                "data_mode": db.data_mode(),
                "data_provider": market_data.provider() if live else "demo",
                "data_label": (market_data.ALPACA_SOURCE if market_data.provider() == "alpaca" else live_data.SOURCE) if live else demo_data.SOURCE,
                "simulated_data": not live, "live_fetch": db.kv_get(c, "live_fetch") if live else None,
                "watchlist": db.kv_get(c, "watchlist", []), "account": trading.account(c),
                "scheduler": SCHED.state() if SCHED else None,
                "stream": STREAM.state if STREAM and STREAM.thread else None,
                "day_monitor": DAYMON.state() if DAYMON else None,
                "kill_switch": risk.kill_switch(c), "reconciliation": db.kv_get(c, "reconciliation"),
                "broker_connected": alpaca.configured(), "environment": db.environment(),
                "live_trading": live_gate.enabled(),
                "last_refresh": db.kv_get(c, "last_refresh") if live else None,
                "fill_model": {"slippage_bps": sim.SLIPPAGE_BPS, "participation_cap": sim.PARTICIPATION}}
    finally:
        c.close()


@app.get("/api/stream")
async def stream_changes(request: Request):
    """Server-Sent Events: tells the UI to refresh when orders, alerts or data change."""
    import asyncio

    from fastapi.responses import StreamingResponse

    async def gen():
        seen, _ = events.current()
        yield f"event: hello\ndata: {seen}\n\n"
        idle = 0
        while not await request.is_disconnected():
            v, reason = events.current()
            if v != seen:
                seen, idle = v, 0
                yield f"data: {json_dumps({'version': v, 'reason': reason})}\n\n"
            else:
                idle += 1
                if idle % 15 == 0:
                    yield ": keepalive\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(gen(), media_type="text/event-stream", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})


def json_dumps(o):
    import json
    return json.dumps(o)


@app.get("/api/watchlist/screen")
def screen():
    with db.tx() as c:   # screening records forward-test signals; keep those writes atomic
        return trading.screen(c)


@app.get("/api/symbols/{symbol}/evaluation")
def evaluation(symbol: str):
    c = conn()
    try:
        return trading.evaluate_symbol(c, symbol.upper())
    finally:
        c.close()


@app.get("/api/symbols/{symbol}/chart")
def chart(symbol: str, sessions: int = 90):
    c = conn()
    try:
        return trading.chart(c, symbol.upper(), sessions)
    finally:
        c.close()


@app.get("/api/symbols/{symbol}/explain")
def explain_symbol(symbol: str, level: str = "beginner"):
    if level not in ("beginner", "analyst"):
        raise HTTPException(400, "level must be beginner or analyst")
    c = conn()
    try:
        p, _ = trading.current_params(c)
        ev = trading.evaluate_symbol(c, symbol.upper(), p)
        from dataclasses import asdict
        return {"explanation": explain.explain(ev, asdict(p), level), "evaluation": ev}
    finally:
        c.close()


@app.get("/api/strategies")
def strategies():
    from .rules import STRATEGIES, RuleParams, stop_rule_text
    return [{"id": k, "name": v["name"], "style": v["style"], "template": v["template"], "params": v["params"],
             "core": list(v["core"]), "defaults": v["defaults"],
             "stop_rule": stop_rule_text(RuleParams(strategy=k, **v["defaults"]))} for k, v in STRATEGIES.items()]


class ParseIn(BaseModel):
    text: str


@app.post("/api/strategy/parse")
def parse(body: ParseIn):
    return trading.draft_ruleset(body.text)


@app.get("/api/rulesets")
def rulesets():
    c = conn()
    try:
        return {"active": trading.active_ruleset(c), "draft": trading.draft_ruleset(), "history": trading.list_rulesets(c)}
    finally:
        c.close()


class RulesetIn(BaseModel):
    text: str
    params: dict
    interpretations: list = Field(default_factory=list)
    name: str = ""


@app.post("/api/rulesets")
def save_ruleset(body: RulesetIn):
    with db.tx() as c:
        return trading.confirm_ruleset(c, body.text, body.params, body.interpretations, body.name)


class PreviewIn(BaseModel):
    symbol: str
    overrides: dict = Field(default_factory=dict)


@app.post("/api/previews")
def preview(body: PreviewIn):
    acct = trading.broker_account()   # network read first, outside the transaction
    with db.tx() as c:
        return trading.make_preview(c, body.symbol.upper(), body.overrides, acct)


class OrderIn(BaseModel):
    preview_id: str
    confirm: bool = False
    thesis: str = ""
    ack_earnings: bool = False
    risk_overrides: dict[str, str] = Field(default_factory=dict)
    live_confirm: str = ""


@app.post("/api/orders")
def submit(body: OrderIn):
    return trading.submit(body.preview_id, body.confirm, body.thesis, body.ack_earnings, body.risk_overrides, body.live_confirm)


@app.get("/api/orders")
def orders():
    c = conn()
    try:
        m = trading.broker_mode(c)
        if factory.is_external(m) and factory.connected(m):
            try:
                broker_sync.sync()
            except Exception:
                pass   # show the last known state; the sync error is recorded under broker status
        ids = [r["id"] for r in c.execute("SELECT id FROM orders ORDER BY created_at DESC")]
        return [trading.order_view(c, i) for i in ids]
    finally:
        c.close()


@app.post("/api/orders/{oid}/cancel")
def cancel(oid: str):
    return trading.cancel_order(oid)


@app.post("/api/orders/{oid}/close")
def close(oid: str):
    c = conn()
    try:
        row = c.execute("SELECT broker FROM orders WHERE id=?", (oid,)).fetchone()
    finally:
        c.close()
    if not row:
        raise HTTPException(404, "Unknown order")
    if factory.is_external(row["broker"]):
        return broker_sync.manual_close(oid)
    with db.tx() as c:
        return trading.close_position(c, oid)


# ---------- broker connection ----------

class ConnectIn(BaseModel):
    key_id: str
    secret: str


def _connection_state() -> dict:
    env = db.environment()
    k, _, source = credentials.get_alpaca(env)
    out = {"provider": "Alpaca" + (" LIVE (real money)" if env == "real" else " Paper"), "connected": bool(k),
           "key_hint": credentials.hint(k), "source": source, "environment": env,
           "base_url": alpaca.LIVE_URL if env == "real" else alpaca.PAPER_URL}
    c = conn()
    try:
        out["broker_mode"] = trading.broker_mode(c)
        out["data_mode"] = db.data_mode()
        out["last_sync"] = db.kv_get(c, "broker_sync")
        out["reconciliation"] = db.kv_get(c, "reconciliation")
    finally:
        c.close()
    if k:
        try:
            out["account"] = alpaca.AlpacaPaperBroker().account()
        except Exception as exc:
            out["account_error"] = str(exc)[:200]
    return out


@app.get("/api/broker/connection")
def connection():
    return _connection_state()


@app.post("/api/broker/connect")
def connect(body: ConnectIn):
    key, secret = body.key_id.strip(), body.secret.strip()
    if not key or not secret:
        raise HTTPException(400, "Enter both the API key ID and the secret.")
    try:
        acct = alpaca.AlpacaPaperBroker(key=key, secret=secret).account()   # test before saving
    except Exception as exc:
        raise HTTPException(400, f"Connection test failed: {exc}")
    try:
        credentials.save_alpaca(key, secret)
    except Exception as exc:
        raise HTTPException(500, f"Couldn't save to the system keychain: {exc}")
    return {**_connection_state(), "tested_account": acct}


@app.delete("/api/broker/connect")
def disconnect():
    credentials.delete_alpaca()
    with db.tx() as c:
        db.kv_set(c, "broker_mode", "simulator")
    return _connection_state()


@app.post("/api/broker/sync")
def broker_sync_now():
    return broker_sync.sync()


@app.post("/api/broker/exits")
def broker_exits_now():
    return {"events": broker_sync.run_planned_exits()}


@app.post("/api/reconcile")
def reconcile_now():
    c = conn()
    try:
        mode = trading.broker_mode(c)
    finally:
        c.close()
    if factory.is_external(mode):
        try:
            broker_sync.sync()
        except Exception:
            pass
    return broker_sync.reconcile(mode)


# ---------- paper-to-live gate ----------

@app.get("/api/live-gate")
def gate_status():
    return live_gate.status()


@app.post("/api/live-gate/keys")
def gate_keys(body: ConnectIn):
    key, secret = body.key_id.strip(), body.secret.strip()
    if not key or not secret:
        raise HTTPException(400, "Enter both the live API key ID and the secret.")
    try:
        acct = alpaca.AlpacaPaperBroker(key=key, secret=secret, env="real", key_test=True).account()   # read-only test
    except Exception as exc:
        raise HTTPException(400, f"Live key test failed: {exc}")
    credentials.save_alpaca(key, secret, env="real")
    return {**live_gate.status(), "tested_account": {k: acct[k] for k in ("status", "equity", "buying_power", "account_number")}}


@app.delete("/api/live-gate/keys")
def gate_keys_delete():
    if db.environment() == "real":
        live_gate.disable()
    credentials.delete_alpaca(env="real")
    return live_gate.status()


class GateIn(BaseModel):
    phrase: str
    broker: str = "alpaca"


@app.post("/api/live-gate/enable")
def gate_enable(body: GateIn):
    out = live_gate.enable(body.phrase, body.broker)
    ensure_db("live")
    try:
        trading.run_live_refresh("live trading enabled")
    except Exception:
        pass
    return out


@app.post("/api/live-gate/disable")
def gate_disable():
    return live_gate.disable()


# ---------- day trading ----------

@app.get("/api/day/state")
def day_state():
    return day_mod.state()


class DaySessionIn(BaseModel):
    mode: str
    date: str | None = None


@app.put("/api/day/session")
def day_session(body: DaySessionIn):
    return day_mod.set_session(body.mode, body.date)


class DayStepIn(BaseModel):
    bars: int = 1


@app.post("/api/day/step")
def day_step(body: DayStepIn):
    return day_mod.step(body.bars)


@app.get("/api/day/screen")
def day_screen():
    return day_mod.screen()


@app.get("/api/day/chart/{symbol}")
def day_chart(symbol: str):
    return day_mod.chart(symbol.upper())


@app.put("/api/day/rules")
def day_rules(body: dict):
    from . import intraday
    with db.tx() as c:
        return intraday.save_params(c, body.get("params", body))


class DayPreviewIn(BaseModel):
    symbol: str
    qty: int | None = None


@app.post("/api/day/preview")
def day_preview(body: DayPreviewIn):
    return day_mod.preview(body.symbol.upper(), body.qty)


class DayOrderIn(BaseModel):
    preview_id: str
    confirm: bool = False
    thesis: str = ""
    risk_overrides: dict[str, str] = Field(default_factory=dict)


@app.post("/api/day/orders")
def day_order(body: DayOrderIn):
    return day_mod.submit(body.preview_id, body.confirm, body.thesis, body.risk_overrides)


@app.post("/api/day/orders/{oid}/flatten")
def day_flatten(oid: str):
    return day_mod.flatten(oid)


@app.get("/api/day/positions")
def day_positions():
    return day_mod.positions()


# ---------- market data ----------

@app.get("/api/data")
def data_state():
    c = conn()
    try:
        return {"provider": market_data.provider(), "providers": market_data.PROVIDERS,
                "quality": db.kv_get(c, "data_quality"), "live_fetch": db.kv_get(c, "live_fetch"),
                "corporate_actions": [dict(r) for r in c.execute("SELECT * FROM corporate_actions ORDER BY ex_date DESC LIMIT 20")]}
    finally:
        c.close()


class ProviderIn(BaseModel):
    provider: str


@app.put("/api/data/provider")
def data_provider(body: ProviderIn):
    try:
        market_data.set_provider(body.provider)
    except ValueError as exc:
        raise HTTPException(409, str(exc))
    out = {"provider": market_data.provider()}
    if db.data_mode() == "live":
        try:
            r = trading.run_live_refresh(f"switched to {body.provider} data")
            out["refresh"] = {k: r.get(k) for k in ("as_of", "provider", "provider_note", "splits", "quality")}
        except Exception as exc:
            out["refresh_error"] = str(exc)[:200]
    return out


# ---------- OAuth connect ----------

@app.get("/api/oauth")
def oauth_status():
    return [oauth.status(p) for p in oauth.PROVIDERS]


@app.get("/api/oauth/{provider}/start")
def oauth_start(provider: str):
    if provider not in oauth.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    try:
        return oauth.start(provider)
    except ValueError as exc:
        raise HTTPException(409, str(exc))


@app.get("/api/oauth/{provider}/callback", response_class=HTMLResponse)
def oauth_callback(provider: str, code: str = "", state: str = "", error: str = ""):
    import html as _h
    if provider not in oauth.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    name = oauth.PROVIDERS[provider]["name"]
    if error or not code:
        msg, ok = f"{name} sign-in was cancelled or failed: {error or 'no code returned'}.", False
    else:
        try:
            oauth.callback(provider, code, state)
            msg, ok = f"Connected to {name}. You can close this tab and return to Tickmark.", True
        except ValueError as exc:
            msg, ok = str(exc), False
    color = "#2fb37a" if ok else "#e0584f"
    return HTMLResponse(f"<!doctype html><meta charset=utf-8><title>Tickmark</title>"
                        f"<body style='font:16px system-ui;background:#0e1115;color:#e4e7eb;display:grid;place-items:center;height:100vh'>"
                        f"<p style='border:1px solid {color};padding:20px;border-radius:8px;max-width:480px'>{_h.escape(msg)}</p>",
                        status_code=200 if ok else 400)


@app.delete("/api/oauth/{provider}")
def oauth_disconnect(provider: str):
    if provider not in oauth.PROVIDERS:
        raise HTTPException(404, "Unknown provider")
    oauth.disconnect(provider)
    return oauth.status(provider)


# ---------- Interactive Brokers gateway ----------

class IBKRIn(BaseModel):
    gateway_url: str | None = None
    account_id: str | None = None


@app.get("/api/broker/ibkr")
def ibkr_status():
    s = ibkr.settings()
    out = {"settings": s, "configured": ibkr.configured(), "paper_only": True}
    try:
        b = ibkr.IBKRBroker(require_account=False)
        out["session"] = b.session()
        out["accounts"] = b.list_accounts() if out["session"]["authenticated"] else []
        if ibkr.configured():
            out["account"] = ibkr.IBKRBroker().account()
    except Exception as exc:
        out["error"] = str(exc)[:700]
    return out


@app.put("/api/broker/ibkr")
def ibkr_settings(body: IBKRIn):
    if body.account_id and not body.account_id.strip().upper().startswith("DU"):
        raise HTTPException(409, "Pick an IBKR paper account (IDs start with DU). Live IBKR trading is not enabled in this build.")
    try:
        ibkr.save_settings(body.gateway_url, body.account_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return ibkr_status()


# ---------- broker catalogue ----------

@app.get("/api/connections")
def connections_overview():
    from . import connections
    return connections.overview()


@app.get("/api/brokers")
def brokers():
    return registry.catalogue()


# ---------- alerts ----------

@app.get("/api/alerts")
def alerts_feed():
    c = conn()
    try:
        return {**alerts.feed(c), "settings": alerts.public_settings()}
    finally:
        c.close()


@app.post("/api/alerts/read")
def alerts_read():
    with db.tx() as c:
        alerts.mark_read(c)
    return {"ok": True}


@app.put("/api/alerts/settings")
def alerts_settings(body: dict):
    return alerts.save_settings(body)


class AlertSecretsIn(BaseModel):
    slack_webhook: str | None = None
    smtp_password: str | None = None


@app.put("/api/alerts/secrets")
def alerts_secrets(body: AlertSecretsIn):
    if body.slack_webhook is not None:
        if body.slack_webhook and not body.slack_webhook.startswith("https://hooks.slack.com/"):
            raise HTTPException(400, "Slack webhook URLs start with https://hooks.slack.com/")
        credentials.set_secret(alerts.SLACK_SECRET, body.slack_webhook.strip())
    if body.smtp_password is not None:
        credentials.set_secret(alerts.SMTP_SECRET, body.smtp_password)
    return alerts.public_settings()


class AlertTestIn(BaseModel):
    channel: str


@app.post("/api/alerts/test")
def alerts_test(body: AlertTestIn):
    if body.channel not in alerts.CHANNELS:
        raise HTTPException(400, "channel must be desktop, slack or email")
    try:
        alerts.send(body.channel, "Test alert", "If you can read this, Tickmark alerts reach you here.")
    except Exception as exc:
        raise HTTPException(502, f"{body.channel} test failed: {exc}")
    return {"ok": True}


# ---------- copilot ----------

class CopilotKeyIn(BaseModel):
    key: str


class CopilotChatIn(BaseModel):
    text: str
    thread_id: str | None = None
    level: str = "beginner"
    model: str | None = None


class CopilotSettingsIn(BaseModel):
    model: str
    local_base_url: str | None = None
    local_model: str | None = None
    openai_model: str | None = None
    gemini_model: str | None = None


@app.get("/api/copilot")
def copilot_status():
    return {**copilot_mod.status(), "threads": copilot_mod.threads()}


@app.put("/api/copilot/key")
def copilot_key(body: CopilotKeyIn):
    return copilot_mod.save_key(body.key)


@app.delete("/api/copilot/key")
def copilot_key_delete():
    return copilot_mod.delete_key()


@app.post("/api/copilot/chat")
def copilot_chat(body: CopilotChatIn):
    return copilot_mod.chat(body.text, body.thread_id, body.level if body.level in ("beginner", "analyst") else "beginner", body.model)


@app.put("/api/copilot/settings")
def copilot_settings(body: CopilotSettingsIn):
    return copilot_mod.save_settings(body.model, body.local_base_url, body.local_model, body.openai_model, body.gemini_model)


@app.put("/api/copilot/providers/{provider}/key")
def copilot_cloud_key(provider: str, body: CopilotKeyIn):
    return copilot_mod.save_cloud_key(provider, body.key)


@app.delete("/api/copilot/providers/{provider}/key")
def copilot_cloud_key_delete(provider: str):
    return copilot_mod.delete_cloud_key(provider)


@app.get("/api/copilot/providers/{provider}/models")
def copilot_cloud_models(provider: str):
    return {"provider": provider, "models": copilot_mod.cloud_models(provider)}


@app.get("/api/copilot/local-models")
def copilot_local_models(base_url: str | None = None):
    return copilot_mod.local_models(base_url)


@app.get("/api/copilot/threads/{tid}")
def copilot_thread(tid: str):
    return copilot_mod.thread(tid)


@app.get("/api/copilot/proposals")
def copilot_pending():
    return copilot_mod.pending()


@app.post("/api/copilot/proposals/{pid}/apply")
def copilot_apply(pid: str):
    return copilot_mod.apply(pid)


@app.post("/api/copilot/proposals/{pid}/dismiss")
def copilot_dismiss(pid: str):
    return copilot_mod.dismiss(pid)


# ---------- linked accounts (read-only) ----------

class LiveSwitchIn(BaseModel):
    on: bool
    phrase: str = ""


class LinkIn(BaseModel):
    provider: str
    label: str = ""
    sandbox: bool = True
    values: dict = Field(default_factory=dict)


@app.get("/api/linked")
def linked_list():
    return {"providers": linked_mod.providers(), **linked_mod.summary()}


@app.post("/api/linked")
def linked_add(body: LinkIn):
    return linked_mod.add(body.provider, body.label, body.sandbox, body.values)


@app.post("/api/linked/{aid}/refresh")
def linked_refresh(aid: str):
    return linked_mod.refresh(aid)


@app.get("/api/linked/{aid}/live")
def linked_live_status(aid: str):
    return linked_mod.live_status(linked_mod.get(aid))


@app.put("/api/linked/{aid}/live")
def linked_live(aid: str, body: LiveSwitchIn):
    return linked_mod.set_live(aid, body.on, body.phrase)


@app.delete("/api/linked/{aid}")
def linked_remove(aid: str):
    return linked_mod.remove(aid)


# ---------- crypto orders (testnet / demo exchange accounts) ----------

class CryptoPreviewIn(BaseModel):
    account_id: str
    symbol: str
    side: str = "buy"
    notional: float | None = None
    amount: float | None = None
    order_type: str = "market"
    limit_price: float | None = None
    stop_price: float | None = None


class CryptoOrderIn(BaseModel):
    preview_id: str
    confirm: bool = False
    thesis: str = ""
    live_confirm: str = ""



@app.get("/api/crypto/journal")
def crypto_journal():
    c = conn()
    try:
        return pnl_mod.crypto_journal(c)
    finally:
        c.close()


@app.get("/api/pnl")
def pnl_stats():
    c = conn()
    try:
        return pnl_mod.stats(c)
    finally:
        c.close()


@app.get("/api/crypto/accounts")
def crypto_accounts():
    return crypto_mod.tradable_accounts()


@app.get("/api/crypto/markets")
def crypto_markets(account_id: str, q: str = ""):
    return crypto_mod.markets(account_id, q)


@app.post("/api/crypto/preview")
def crypto_preview(body: CryptoPreviewIn):
    return crypto_mod.preview(body.account_id, body.symbol, body.side, body.notional, body.amount, body.order_type,
                              body.limit_price, body.stop_price)


@app.post("/api/crypto/orders")
def crypto_submit(body: CryptoOrderIn):
    return crypto_mod.submit(body.preview_id, body.confirm, body.thesis, body.live_confirm)


@app.get("/api/crypto/orders")
def crypto_orders():
    try:
        crypto_mod.sync()
    except Exception:
        pass        # show the last known state
    return crypto_mod.list_orders()


@app.post("/api/crypto/orders/{oid}/cancel")
def crypto_cancel(oid: str):
    return crypto_mod.cancel(oid)


@app.post("/api/crypto/orders/{oid}/close")
def crypto_close(oid: str):
    return crypto_mod.close(oid)


# ---------- risk ----------

@app.get("/api/risk")
def risk_state():
    c = conn()
    try:
        return {"limits": risk.limits(c), "history": risk.history(c), "kill_switch": risk.kill_switch(c),
                "overrides": risk.override_log(c), "defaults": risk.DEFAULTS}
    finally:
        c.close()


class LimitsIn(BaseModel):
    params: dict
    note: str = ""


@app.put("/api/risk/limits")
def risk_limits(body: LimitsIn):
    with db.tx() as c:
        return risk.save_limits(c, body.params, body.note)


class KillIn(BaseModel):
    on: bool
    reason: str = ""


@app.put("/api/risk/kill-switch")
def kill(body: KillIn):
    with db.tx() as c:
        return risk.set_kill_switch(c, body.on, body.reason)


@app.post("/api/sim/advance")
def advance():
    with db.tx() as c:
        if trading.broker_mode(c) != "simulator":
            raise TradeError("The simulator clock only runs in Local Simulator mode.", 409)
        return trading.advance(c)


@app.post("/api/sim/reset")
def reset():
    # executescript() manages its own commits, so no outer transaction here
    ensure_db(db.data_mode(), reset=True)
    return {"ok": True, "data_mode": db.data_mode()}


class DataModeIn(BaseModel):
    mode: str


@app.put("/api/data/mode")
def set_data_mode(body: DataModeIn):
    if body.mode == "demo" and db.environment() == "real":
        live_gate.disable()   # simulated prices and real money never mix
    db.set_data_mode(body.mode)
    ensure_db(body.mode)
    if body.mode == "demo":  # the broker must never act on simulated prices
        with db.tx() as c:
            db.kv_set(c, "broker_mode", "simulator")
    out = {"data_mode": body.mode}
    if body.mode == "live":
        c = db.connect()
        try:
            empty = trading.as_of(c) is None
        finally:
            c.close()
        if empty:
            out["refresh"] = trading.run_live_refresh("first switch to live")
    return out


class SchedIn(BaseModel):
    enabled: bool


@app.get("/api/scheduler")
def sched_state():
    return SCHED.state() if SCHED else {"enabled": scheduler.enabled(), "thread_alive": False}


@app.put("/api/scheduler")
def sched_set(body: SchedIn):
    db.set_setting("auto_refresh", body.enabled)
    return sched_state()


@app.post("/api/live/refresh")
def live_refresh():
    return trading.run_live_refresh("manual")


class WatchlistIn(BaseModel):
    symbols: list[str]


@app.put("/api/watchlist")
def set_watchlist(body: WatchlistIn):
    with db.tx() as c:
        syms = trading.set_watchlist(c, body.symbols)
    res = trading.run_live_refresh("watchlist change")
    bad = {k: v["error"] for k, v in res["report"].items() if not v["ok"]}
    return {"watchlist": syms, "errors": bad, "as_of": res["as_of"]}


@app.get("/api/forward")
def forward_test():
    with db.tx() as c:
        return trading.forward_summary(c)


class ModeIn(BaseModel):
    mode: str


@app.put("/api/broker/mode")
def set_mode(body: ModeIn):
    if body.mode not in ("simulator", *factory.EXTERNAL):
        raise HTTPException(400, f"mode must be one of simulator, {', '.join(factory.EXTERNAL)}")
    if factory.is_external(body.mode) and not factory.connected(body.mode):
        raise HTTPException(409, f"{factory.LABELS[body.mode]} is not connected yet. Connect it in the Broker tab first.")
    if db.environment() == "real" and factory.is_external(body.mode) and not factory.capabilities(body.mode)["real_money"]:
        raise HTTPException(409, f"{factory.LABELS[body.mode]} is paper-only in this build. Switch back to paper first.")
    if body.mode == "simulator" and db.environment() == "real":
        raise HTTPException(409, "Real-money mode always uses the broker. Switch back to paper first.")
    if factory.is_external(body.mode) and db.data_mode() != "live":
        raise HTTPException(409, "Broker orders need Live data: sizing and stops from simulated prices would be wrong for real symbols.")
    with db.tx() as c:
        db.kv_set(c, "broker_mode", body.mode)
    return {"mode": body.mode}


@app.get("/api/journal")
def journal():
    c = conn()
    try:
        return trading.journal(c)
    finally:
        c.close()


class NoteIn(BaseModel):
    note: str


@app.post("/api/journal/{jid}/note")
def note(jid: int, body: NoteIn):
    with db.tx() as c:
        trading.add_review_note(c, jid, body.note)
    return {"ok": True}


@app.get("/api/backtest")
def run_backtest():
    c = conn()
    try:
        p, rs = trading.current_params(c)
        syms = db.kv_get(c, "watchlist", [])
        series = trading.all_series(c)   # skips tickers with no bars instead of failing the whole replay
        if not series:
            raise TradeError("No market data loaded yet.", 409)
        earn = {s: [r["date"] for r in c.execute("SELECT date FROM earnings WHERE symbol=? AND date IS NOT NULL", (s,))] for s in syms}
        out = backtest.run(series, p, earn)
        out["ruleset_version"] = rs["version"]
        out["data_label"] = live_data.SOURCE if db.data_mode() == "live" else demo_data.SOURCE
        return out
    finally:
        c.close()


@app.get("/api/documents/samples")
def doc_samples():
    return {"a": documents.SAMPLE_A, "b": documents.SAMPLE_B,
            "label": "Fictional sample releases for a made-up company. Paste real releases to analyse them."}


class CompareIn(BaseModel):
    doc_a: str
    doc_b: str
    question: str = ""
    symbol: str | None = None


@app.post("/api/documents/compare")
def compare(body: CompareIn):
    if not body.doc_a.strip() or not body.doc_b.strip():
        raise HTTPException(400, "Paste two documents to compare.")
    window = None
    if body.symbol:
        c = conn()
        try:
            window = trading.evaluate_symbol(c, body.symbol.upper())["plan"]
        finally:
            c.close()
    return documents.compare(body.doc_a, body.doc_b, window, body.question)


# Serve the built frontend if present (single-process demo).
DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if DIST.exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")

    @app.get("/{path:path}")
    def spa(path: str):
        if path.startswith("api/"):   # unknown API routes must 404, never return the web page
            raise HTTPException(404, f"No such endpoint: /{path}")
        return FileResponse(DIST / "index.html")
