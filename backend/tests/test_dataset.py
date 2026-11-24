import io
import json
import sqlite3
import zipfile

from test_portal import P, signup  # noqa: F401  (fixture + helper)


def test_admin_dataset_has_every_account_and_what_they_entered_and_no_secrets(P, monkeypatch, tmp_path):
    monkeypatch.setenv("SWINGDESK_OPEN_SIGNUP", "on")
    admin = P()
    signup(admin, "owner@example.com")                          # no setup code in tests: first account is admin
    bob = P()
    signup(bob, "bob@example.com")
    assert bob.put("/api/watchlist", json={"symbols": ["NVDA", "SNDK"]}).status_code == 200
    bob.post("/api/tradingview/token")
    from app import db, portal
    with portal.as_user(bob.get("/api/auth/status").json()["user"]["id"]):
        db.set_setting("my_api_key", "should-never-export")                     # planted secrets
        db.set_setting("copilot", {"model": "oss", "anthropic_token": "sk-should-never-export"})
    g = P()
    g.post("/api/auth/guest")
    assert bob.get("/api/admin/dataset").status_code == 403                         # admin only

    r = admin.get("/api/admin/dataset")
    assert r.status_code == 200 and r.headers["content-disposition"].endswith('.sqlite"')
    f = tmp_path / "d.sqlite"
    f.write_bytes(r.content)
    c = sqlite3.connect(f)
    accts = c.execute("SELECT email, role FROM accounts ORDER BY created_at").fetchall()
    assert accts[:2] == [("owner@example.com", "admin"), ("bob@example.com", "user")] and accts[2] == (None, "guest")
    cols = {row[1] for row in c.execute("PRAGMA table_info(accounts)")}
    assert "pw_hash" not in cols and "email" in cols
    wl = c.execute("SELECT value FROM desk_values WHERE key='watchlist' AND desk='live' AND user_id=(SELECT user_id FROM accounts WHERE email='bob@example.com')").fetchone()
    assert json.loads(wl[0]) == ["NVDA", "SNDK"]
    dump = "\n".join(c.iterdump())
    assert "should-never-export" not in dump and "tvh_" not in dump and "scrypt$" not in dump
    assert "sessions" not in {t for (t,) in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert not c.execute("SELECT name FROM sqlite_master WHERE name='bars'").fetchone()           # market data left out

    r = admin.get("/api/admin/dataset?format=csv&guests=0")
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert "accounts.csv" in z.namelist()
    assert "guest" not in z.read("accounts.csv").decode()
