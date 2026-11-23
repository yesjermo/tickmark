"""Encrypted off-site backup: a wiped disk gets its accounts and data back; GitHub only sees ciphertext."""
import base64
import json
import shutil

import httpx
import pytest
from fastapi.testclient import TestClient

from app import backup, db, portal


class FakeGitHub:
    def __init__(self):
        self.files, self.puts = {}, 0

    def handle(self, req: httpx.Request):
        assert req.headers["authorization"] == "Bearer ghtok"
        path = req.url.path.split("/contents/")[-1]
        if req.method == "GET":
            if path not in self.files:
                return httpx.Response(404, json={"message": "Not Found"})
            if req.headers.get("accept") == "application/vnd.github.raw":
                return httpx.Response(200, content=self.files[path])
            return httpx.Response(200, json={"sha": "abc", "size": len(self.files[path])})
        if req.method == "PUT":
            self.files[path] = base64.b64decode(json.loads(req.content)["content"])
            self.puts += 1
            return httpx.Response(201, json={"content": {"sha": "def"}})
        return httpx.Response(405)


@pytest.fixture
def gh(monkeypatch):
    f = FakeGitHub()
    monkeypatch.setenv("SWINGDESK_BACKUP_REPO", "me/tickmark-data")
    monkeypatch.setenv("SWINGDESK_BACKUP_TOKEN", "ghtok")
    monkeypatch.setenv("SWINGDESK_MASTER_KEY", "any host generated secret value 123")
    monkeypatch.setattr(backup, "TRANSPORT", httpx.MockTransport(f.handle))
    return f


def test_signup_survives_a_wiped_disk(client, gh, monkeypatch):
    monkeypatch.setenv("SWINGDESK_PORTAL", "on")
    monkeypatch.setattr(portal, "_fail", {})
    a = TestClient(client.app, headers={"x-swingdesk": "1"})
    u = a.post("/api/auth/signup", json={"email": "owner@example.com", "password": "correct horse battery"}).json()["user"]
    a.put("/api/risk/kill-switch", json={"on": True, "reason": "remember me after a wipe"})
    backup.BACKUP.maybe_push(force=True)
    blob = gh.files["tickmark-data.bin"]
    assert b"owner@example.com" not in blob and b"SQLite" not in blob            # ciphertext only
    shutil.rmtree(db.DATA_DIR)                                                     # Render's free disk after a sleep
    assert backup.restore_if_empty() == "restored"
    assert backup.restore_if_empty() == "data present; nothing restored"           # never overwrites live data
    b = TestClient(client.app, headers={"x-swingdesk": "1"})
    assert b.post("/api/auth/login", json={"email": "owner@example.com", "password": "correct horse battery"}).status_code == 200
    assert b.get("/api/risk").json()["kill_switch"]["on"] is True
    assert (db.DATA_DIR / "users" / u["id"]).is_dir()


def test_backup_is_off_without_settings_and_no_backup_yet_is_fine(client, monkeypatch):
    monkeypatch.delenv("SWINGDESK_BACKUP_REPO", raising=False)
    assert backup.restore_if_empty() == "backup off"


def test_first_start_with_no_backup(client, gh):
    shutil.rmtree(db.DATA_DIR)
    assert backup.restore_if_empty() == "no backup yet"


def test_failed_restore_pauses_pushes_so_the_good_backup_survives(tmp_path, monkeypatch):
    import pytest
    from app import backup, db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setenv("SWINGDESK_BACKUP_REPO", "me/data")
    monkeypatch.setenv("SWINGDESK_BACKUP_TOKEN", "t")
    monkeypatch.setenv("SWINGDESK_MASTER_KEY", "k" * 40)
    monkeypatch.setattr(backup, "BLOCKED", None)
    puts = []

    def handle(req):
        if req.method == "PUT":
            puts.append(1)
            return httpx.Response(201, json={})
        return httpx.Response(502, text="bad gateway")
    monkeypatch.setattr(backup, "TRANSPORT", httpx.MockTransport(handle))
    with pytest.raises(RuntimeError):
        backup.restore_if_empty()
    b = backup.Backup()
    (tmp_path / "x.db").write_bytes(b"")
    b.maybe_push(force=True)
    assert puts == [] and "restore failed" in b.state["paused"]


def test_backup_pauses_without_a_fixed_master_key(tmp_path, monkeypatch):
    from app import backup, db
    monkeypatch.setattr(db, "DATA_DIR", tmp_path)
    monkeypatch.setenv("SWINGDESK_BACKUP_REPO", "me/data")
    monkeypatch.setenv("SWINGDESK_BACKUP_TOKEN", "t")
    monkeypatch.delenv("SWINGDESK_MASTER_KEY", raising=False)
    monkeypatch.setattr(backup, "BLOCKED", None)
    assert backup.restore_if_empty().startswith("backup paused")


def test_admin_can_see_backup_status_and_back_up_now(client, monkeypatch):
    monkeypatch.setenv("SWINGDESK_BACKUP_REPO", "me/data")
    monkeypatch.setenv("SWINGDESK_BACKUP_TOKEN", "t")
    monkeypatch.setenv("SWINGDESK_MASTER_KEY", "k" * 40)
    monkeypatch.setattr(backup, "BLOCKED", None)
    puts = []

    def handle(req):
        if req.method == "PUT":
            puts.append(json.loads(req.content)["message"])
            return httpx.Response(201, json={})
        return httpx.Response(404)
    monkeypatch.setattr(backup, "TRANSPORT", httpx.MockTransport(handle))
    st = client.get("/api/admin/backup").json()
    assert st["enabled"] and st["repo"] == "me/data" and st["paused"] is None and st["has_master_key"]
    st = client.post("/api/admin/backup/now").json()
    assert puts and st.get("last_ok")
    monkeypatch.delenv("SWINGDESK_BACKUP_TOKEN")
    assert client.post("/api/admin/backup/now").status_code == 409
