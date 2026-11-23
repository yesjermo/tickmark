import json
import os
import sqlite3
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path

DATA_DIR = Path(os.environ.get("SWINGDESK_DATA_DIR", Path(__file__).resolve().parent.parent / "data"))
MODES = ("demo", "live")

# Multi-user portal: each signed-in user's databases, settings and encrypted secrets live in their own folder.
# The request middleware (and background jobs, per user) set this; None means the single-user local app.
TENANT: ContextVar[Path | None] = ContextVar("swingdesk_tenant_dir", default=None)


def data_dir() -> Path:
    return TENANT.get() or DATA_DIR


def _settings() -> dict:
    try:
        return json.loads((data_dir() / "settings.json").read_text())
    except (OSError, ValueError):
        return {}


def get_setting(key: str, default=None):
    return _settings().get(key, default)


def set_setting(key: str, value) -> None:
    data_dir().mkdir(parents=True, exist_ok=True)
    s = _settings()
    s[key] = value
    (data_dir() / "settings.json").write_text(json.dumps(s))


def data_mode() -> str:
    """'demo' = seeded simulated prices; 'live' = real completed daily bars from Yahoo Finance.
    Each mode has its own SQLite file, so live paper trades never mix with demo ones."""
    m = get_setting("data_mode", "demo")
    return m if m in MODES else "demo"


def set_data_mode(mode: str) -> None:
    if mode not in MODES:
        raise ValueError("data mode must be demo or live")
    set_setting("data_mode", mode)


ENVIRONMENTS = ("paper", "real")


def environment() -> str:
    """'paper' (default) or 'real'. Real money is only reachable through app/live_gate.py."""
    e = get_setting("environment", "paper")
    return e if e in ENVIRONMENTS else "paper"


def db_path(mode: str | None = None, env: str | None = None) -> Path:
    m = mode or data_mode()
    if m == "live" and (env or environment()) == "real":
        return data_dir() / "swingdesk-real-money.db"   # real-money journal never mixes with paper
    return data_dir() / f"swingdesk-{m}.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS bars (
  symbol TEXT NOT NULL, date TEXT NOT NULL,
  open REAL, high REAL, low REAL, close REAL, volume REAL,
  source TEXT NOT NULL,
  PRIMARY KEY (symbol, date)
);
CREATE TABLE IF NOT EXISTS earnings (
  symbol TEXT NOT NULL, date TEXT,            -- NULL date = next report date unknown
  kind TEXT NOT NULL,                          -- 'past' | 'next'
  source TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS rulesets (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  version INTEGER NOT NULL UNIQUE,
  name TEXT NOT NULL,
  source_text TEXT NOT NULL,
  params_json TEXT NOT NULL,
  interpretations_json TEXT NOT NULL,
  confirmed_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS previews (
  id TEXT PRIMARY KEY,
  payload_json TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
  id TEXT PRIMARY KEY,
  client_order_id TEXT NOT NULL UNIQUE,        -- = preview id for entries; blocks duplicate submits
  broker TEXT NOT NULL,
  broker_order_id TEXT,
  symbol TEXT NOT NULL, side TEXT NOT NULL, qty INTEGER NOT NULL,
  order_type TEXT NOT NULL, limit_price REAL, stop_price REAL,
  purpose TEXT NOT NULL,                        -- entry | stop | plan_exit | manual_exit
  parent_id TEXT,
  status TEXT NOT NULL,                         -- pending | partially_filled | filled | canceled | rejected
  status_reason TEXT,
  filled_qty INTEGER NOT NULL DEFAULT 0,
  avg_fill_price REAL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS fills (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT NOT NULL, session_date TEXT NOT NULL,
  qty INTEGER NOT NULL, price REAL NOT NULL, note TEXT
);
CREATE TABLE IF NOT EXISTS journal (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  order_id TEXT NOT NULL UNIQUE,
  symbol TEXT NOT NULL,
  created_at TEXT NOT NULL,
  thesis TEXT NOT NULL,
  ruleset_version INTEGER NOT NULL,
  ruleset_json TEXT NOT NULL,
  evidence_json TEXT NOT NULL,
  data_timestamp TEXT NOT NULL,
  order_json TEXT NOT NULL,
  plan_json TEXT NOT NULL
);
-- Entry reasoning is write-once: what the trader knew at the time cannot be edited later.
CREATE TRIGGER IF NOT EXISTS journal_immutable BEFORE UPDATE ON journal
BEGIN SELECT RAISE(ABORT, 'journal entry reasoning is immutable'); END;
-- Forward test: a signal is recorded only when the app actually sees it on the latest
-- completed bar, so results accumulate going forward with no hindsight.
CREATE TABLE IF NOT EXISTS forward_signals (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  symbol TEXT NOT NULL, signal_date TEXT NOT NULL, ruleset_version INTEGER NOT NULL,
  status_at_signal TEXT NOT NULL, evidence_json TEXT NOT NULL, plan_json TEXT NOT NULL,
  stop REAL NOT NULL, recorded_at TEXT NOT NULL, data_source TEXT NOT NULL,
  state TEXT NOT NULL DEFAULT 'waiting_entry',   -- waiting_entry | open | closed
  entry_date TEXT, entry_price REAL, last_date TEXT, last_close REAL,
  exit_date TEXT, exit_price REAL, exit_reason TEXT, return_pct REAL, bench_return_pct REAL,
  UNIQUE(symbol, signal_date, ruleset_version)
);
CREATE TABLE IF NOT EXISTS risk_limits (
  version INTEGER PRIMARY KEY, params_json TEXT NOT NULL, note TEXT, created_at TEXT NOT NULL
);
-- Every soft-block override and kill-switch change, write-once.
CREATE TABLE IF NOT EXISTS risk_overrides (
  id INTEGER PRIMARY KEY AUTOINCREMENT, client_order_id TEXT NOT NULL, symbol TEXT NOT NULL, check_id TEXT NOT NULL,
  observed TEXT, threshold TEXT, reason TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS risk_overrides_immutable BEFORE UPDATE ON risk_overrides
BEGIN SELECT RAISE(ABORT, 'risk override log is append-only'); END;
CREATE TRIGGER IF NOT EXISTS risk_overrides_nodelete BEFORE DELETE ON risk_overrides
BEGIN SELECT RAISE(ABORT, 'risk override log is append-only'); END;
CREATE TABLE IF NOT EXISTS alerts (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL,
  title TEXT NOT NULL, body TEXT, symbol TEXT, dedup_key TEXT UNIQUE, read INTEGER NOT NULL DEFAULT 0,
  delivered_json TEXT
);
CREATE TABLE IF NOT EXISTS corporate_actions (
  id TEXT PRIMARY KEY, symbol TEXT NOT NULL, kind TEXT NOT NULL, ratio REAL, ex_date TEXT NOT NULL,
  label TEXT, source TEXT, applied_at TEXT NOT NULL, touched INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS intraday_bars (
  symbol TEXT NOT NULL, ts TEXT NOT NULL,                -- bar START time, ET, ISO
  day TEXT NOT NULL, open REAL, high REAL, low REAL, close REAL, volume REAL, source TEXT NOT NULL,
  PRIMARY KEY (symbol, ts)
);
CREATE TABLE IF NOT EXISTS day_rulesets (
  version INTEGER PRIMARY KEY, params_json TEXT NOT NULL, created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS sectors (symbol TEXT PRIMARY KEY, sector TEXT, source TEXT, updated_at TEXT);
CREATE TABLE IF NOT EXISTS crypto_orders (
  id TEXT PRIMARY KEY,
  client_order_id TEXT NOT NULL UNIQUE,          -- the preview id: blocks duplicate submits
  account_id TEXT NOT NULL, provider TEXT NOT NULL, environment TEXT NOT NULL,
  symbol TEXT NOT NULL, side TEXT NOT NULL, order_type TEXT NOT NULL,   -- market | limit | stop
  purpose TEXT NOT NULL,                          -- entry | stop | exit
  parent_id TEXT, amount REAL NOT NULL, limit_price REAL, stop_price REAL,
  exchange_order_id TEXT, status TEXT NOT NULL,   -- pending | open | partially_filled | filled | canceled | rejected | expired
  filled REAL NOT NULL DEFAULT 0, average REAL, cost REAL, fee TEXT,
  reason TEXT, thesis TEXT, preview_json TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS copilot_threads (
  id TEXT PRIMARY KEY, title TEXT, messages_json TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS copilot_proposals (
  id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, kind TEXT NOT NULL,   -- rules | risk_limits | swing_order | day_trade | exit
  title TEXT NOT NULL, rationale TEXT, payload_json TEXT NOT NULL,
  status TEXT NOT NULL,                                                 -- proposed | applied | opened | dismissed | failed
  result TEXT, created_at TEXT NOT NULL, decided_at TEXT
);
CREATE TABLE IF NOT EXISTS journal_outcomes (
  journal_id INTEGER PRIMARY KEY,
  status TEXT NOT NULL,                         -- open | closed | not_filled
  entry_date TEXT, entry_price REAL,
  exit_date TEXT, exit_price REAL, exit_reason TEXT,
  qty INTEGER, realized_pnl REAL, return_pct REAL,
  adherence_json TEXT, review_note TEXT, updated_at TEXT NOT NULL
);
"""


def connect(mode: str | None = None, env: str | None = None) -> sqlite3.Connection:
    data_dir().mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path(mode, env), isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


@contextmanager
def tx():
    conn = connect()
    try:
        conn.execute("BEGIN IMMEDIATE")
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise
    finally:
        conn.close()


MIGRATIONS = [
    # eligible_from: first session whose open an order may fill at (no fills at an open that printed before the order existed)
    "ALTER TABLE orders ADD COLUMN eligible_from TEXT",
    "ALTER TABLE forward_signals ADD COLUMN eligible_from TEXT",
    "ALTER TABLE orders ADD COLUMN style TEXT NOT NULL DEFAULT 'swing'",   # 'swing' (daily) | 'intraday'
    "ALTER TABLE orders ADD COLUMN target_price REAL",
    "ALTER TABLE copilot_threads ADD COLUMN model TEXT",
]


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)
    for m in MIGRATIONS:
        try:
            conn.execute(m)
        except sqlite3.OperationalError:
            pass  # column already exists


def kv_get(conn, key, default=None):
    row = conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
    return json.loads(row["value"]) if row else default


def kv_set(conn, key, value):
    conn.execute("INSERT INTO kv(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (key, json.dumps(value)))
