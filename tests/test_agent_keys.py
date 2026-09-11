import asyncio
import hashlib
import json
import os
import sqlite3

from aiohttp.test_utils import TestClient, TestServer

from hud.server import create_app
from hud.store import HUDStore

_CONTENT_GRANTS = {
    "agent_id": "content",
    "allowed_role_refs": ["content"],
    "allowed_tools": ["hud.brief", "hud.ingest", "hud.project"],
    "allowed_google_targets": ["calendar", "tasks"],
    "calendar_id": "primary",
    "tasklist_id": "@default",
}


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUD_ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("HUD_REQUIRE_POST_ONBOARDING_PUSH", "0")
    monkeypatch.setenv("HUD_DB_PATH", str(tmp_path / "hud.db"))
    soul = tmp_path / "soul.md"
    soul.write_text(
        "## Roles Matrix\n| Role Slug | Role Name | Brief Description |\n"
        "| --- | --- | --- |\n| owner | Owner | Lead |\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HUD_SOUL_MD_PATH", str(soul))


def test_agent_keys_table_created_on_init(tmp_path):
    store = HUDStore(str(tmp_path / "hud.db"))
    conn = sqlite3.connect(store.db_path)
    try:
        cols = {row[1] for row in conn.execute("PRAGMA table_info(hud_agent_keys)").fetchall()}
    finally:
        conn.close()
    assert "agent_id" in cols
    assert "key_hash" in cols
    assert "grants_json" in cols
    assert "created_at" in cols
    assert "revoked_at" in cols


def test_admin_provision_returns_plaintext_once(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    app = create_app()

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/agents/keys",
                headers={"X-HUD-Admin-Key": "test-admin-key"},
                json=_CONTENT_GRANTS,
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            data = body["data"]
            plaintext = data["key"]
            assert isinstance(plaintext, str) and len(plaintext) >= 16
            assert data["agent_id"] == "content"
            grants = data.get("grants") or data
            assert grants["allowed_role_refs"] == ["content"]
            assert grants["allowed_tools"] == ["hud.brief", "hud.ingest", "hud.project"]
            assert grants["allowed_google_targets"] == ["calendar", "tasks"]
            assert grants.get("calendar_id") == "primary"
            assert grants.get("tasklist_id") == "@default"

            conn = sqlite3.connect(os.environ["HUD_DB_PATH"])
            try:
                row = conn.execute(
                    "SELECT agent_id, key_hash, grants_json FROM hud_agent_keys WHERE agent_id = ?",
                    ("content",),
                ).fetchone()
            finally:
                conn.close()
            assert row is not None
            assert row[0] == "content"
            expected_hash = hashlib.sha256(plaintext.encode("utf-8")).hexdigest()
            assert row[1] == expected_hash
            assert plaintext not in row[1]
            stored_grants = json.loads(row[2])
            assert plaintext not in row[2]
            assert stored_grants["agent_id"] == "content"
            assert stored_grants["allowed_role_refs"] == ["content"]
            assert stored_grants["allowed_tools"] == ["hud.brief", "hud.ingest", "hud.project"]
            assert stored_grants["allowed_google_targets"] == ["calendar", "tasks"]
            assert stored_grants.get("calendar_id") == "primary"
            assert stored_grants.get("tasklist_id") == "@default"

            listed = HUDStore(os.environ["HUD_DB_PATH"]).get_agent_key("content")
            assert listed is not None
            assert "key" not in listed
            assert listed.get("key_hash") == expected_hash
            assert plaintext not in json.dumps(listed)
        finally:
            await client.close()

    asyncio.run(run())


def test_agent_cannot_provision(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    app = create_app()

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/agents/keys",
                headers={"X-HUD-Agent-Key": "not-an-admin-key"},
                json=_CONTENT_GRANTS,
            )
            assert resp.status == 403
            body = await resp.json()
            assert body["error"]["type"] == "authorization_error"
            assert body["error"]["code"] == "hud_agent_forbidden"
        finally:
            await client.close()

    asyncio.run(run())


def test_anonymous_provision_401(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    app = create_app()

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/hud/agents/keys", json=_CONTENT_GRANTS)
            assert resp.status == 401
            body = await resp.json()
            assert body["error"]["code"] == "invalid_hud_admin_key"
            assert body["error"]["type"] == "authentication_error"
        finally:
            await client.close()

    asyncio.run(run())
