"""Tickmark as an MCP server (Model Context Protocol, Streamable HTTP transport, JSON responses).

AI apps (Claude Code, Claude Desktop, other MCP clients) connect to POST /mcp with a personal access token
(Authorization: Bearer sdk_...) made in the app. They get exactly the copilot's tools:
  * get_* tools read the desk (screen, evidence, indicators, positions, journal, risk, day trading, linked
    accounts, crypto orders)
  * propose_* tools create proposals that appear in the app's Copilot tab, where the user applies or confirms them
No tool can place, cancel or close an order, override a risk block, or change keys or settings, so an AI app
holding a token can research and draft but never trade.

Implements: initialize, notifications/initialized, ping, tools/list, tools/call (single and batched JSON-RPC).
The Origin header is checked (DNS-rebinding protection) as the MCP spec requires.
"""
from __future__ import annotations

import json

from . import copilot

PROTOCOLS = ("2025-06-18", "2025-03-26", "2024-11-05")
SERVER = {"name": "tickmark", "title": "Tickmark", "version": "1.0"}
INSTRUCTIONS = ("Tickmark is a swing- and day-trading research desk. Call get_overview first. Quote numbers only from tool "
                "results. propose_* tools create proposals the user reviews in the Tickmark app; you cannot place, cancel "
                "or close orders. This is decision support, not financial advice.")


def tools() -> list[dict]:
    return [{"name": t["name"], "description": t["description"], "inputSchema": t["input_schema"],
             "annotations": {"readOnlyHint": t["name"].startswith(("get_", "list_")), "destructiveHint": False,
                             "openWorldHint": False}} for t in copilot.TOOLS]


def _err(id_, code, msg):
    return {"jsonrpc": "2.0", "id": id_, "error": {"code": code, "message": msg}}


def handle_one(msg: dict, token_id: str) -> dict | None:
    if not isinstance(msg, dict) or msg.get("jsonrpc") != "2.0" or "method" not in msg:
        return _err(msg.get("id") if isinstance(msg, dict) else None, -32600, "Invalid Request")
    method, id_, params = msg["method"], msg.get("id"), msg.get("params") or {}
    if id_ is None:                      # notification (e.g. notifications/initialized): no response
        return None
    if method == "initialize":
        want = params.get("protocolVersion")
        return {"jsonrpc": "2.0", "id": id_, "result": {
            "protocolVersion": want if want in PROTOCOLS else PROTOCOLS[0],
            "capabilities": {"tools": {"listChanged": False}}, "serverInfo": SERVER, "instructions": INSTRUCTIONS}}
    if method == "ping":
        return {"jsonrpc": "2.0", "id": id_, "result": {}}
    if method == "tools/list":
        return {"jsonrpc": "2.0", "id": id_, "result": {"tools": tools()}}
    if method == "tools/call":
        name, args = params.get("name"), params.get("arguments") or {}
        if name not in {t["name"] for t in copilot.TOOLS}:
            return _err(id_, -32602, f"Unknown tool: {name}")
        res, prop = copilot.run_tool(name, args, f"mcp:{token_id}")
        text = json.dumps(res, default=str)
        if prop:
            text = json.dumps({**res, "next_step": "The user reviews this proposal in the Tickmark app (Copilot tab)."}, default=str)
        return {"jsonrpc": "2.0", "id": id_, "result": {"content": [{"type": "text", "text": text[:60000]}], "isError": "error" in res}}
    return _err(id_, -32601, f"Method not found: {method}")


def handle(body, token_id: str):
    if isinstance(body, list):
        out = [r for r in (handle_one(m, token_id) for m in body) if r is not None]
        return out or None
    return handle_one(body, token_id)
