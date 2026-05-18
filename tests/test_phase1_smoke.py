import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hud.server import create_app
from hud.store import HUDStore

_MINIMAL_VALID_SOUL_MD = """## Part 12 Roles
| Role Slug | Role Name | Brief Description |
| --- | --- | --- |
| owner | Owner | Lead |

## Part 13 Goals Per Role
| Role | Goal | Done |
| --- | --- | --- |
| Owner | Ship v1 | Q1 |
"""


@pytest.fixture(autouse=True)
def _hud_phase1_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUD_ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("HUD_REQUIRE_POST_ONBOARDING_PUSH", "0")
    monkeypatch.setenv("HUD_DB_PATH", str(tmp_path / "hud.db"))
    soul = tmp_path / "soul.md"
    soul.write_text(
        "## Part 12 Roles\n| Role Slug | Role Name | Brief Description |\n"
        "| --- | --- | --- |\n| owner | Owner | Lead |\n\n"
        "## Part 13 Goals Per Role\n| Role | Goal | Done |\n"
        "| --- | --- | --- |\n| Owner | Ship v1 | Q1 |\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("HUD_SOUL_MD_PATH", str(soul))


def _admin_headers():
    return {"X-HUD-Admin-Key": "test-admin-key"}


@pytest.fixture
def app(tmp_path):
    return create_app()


async def _get(client: TestClient, path: str):
    return await client.get(path)


async def _post(client: TestClient, path: str, *, json=None):
    return await client.post(path, headers=_admin_headers(), json=json)


def test_health_endpoint(app):
    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await _get(client, "/health")
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            assert body["service"] == "AdventedHUD"
        finally:
            await client.close()

    asyncio.run(run())


def test_mcp_method_not_found(app):
    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await _post(
                client,
                "/hud/mcp",
                json={"jsonrpc": "2.0", "method": "hud.nonexistent"},
            )
            assert resp.status == 404
            body = await resp.json()
            assert body["status"] == "error"
            assert body["error"]["code"] == "method_not_found"
        finally:
            await client.close()

    asyncio.run(run())


def test_sync_status_with_store_item(app, tmp_path):
    store = HUDStore(str(tmp_path / "hud.db"))
    store.upsert_item(
        {
            "internal_id": "smoke-1",
            "actor": "alice",
            "intent": "ingest",
            "scope": "today",
            "status": "pending",
            "payload_json": {"k": "v"},
        }
    )

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await _post(client, "/hud/sync_status")
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            assert body["route"] == "/hud/sync_status"
            assert body["data"]["summary"]["total"] >= 1
            assert any(i["internal_id"] == "smoke-1" for i in body["data"]["items"])
        finally:
            await client.close()

    asyncio.run(run())


def test_mcp_brief_worker_backed(app):
    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await _post(
                client,
                "/hud/mcp",
                json={"jsonrpc": "2.0", "method": "hud.brief"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            assert body["data"]["mode"] == "classification_context"
            assert "classification" in body["data"]
            assert "projection" in body["data"]
            assert body["data"]["classification"].get("intent") == "brief"
        finally:
            await client.close()

    asyncio.run(run())


def test_mcp_ingest_creates_item(app):
    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await _post(
                client,
                "/hud/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "hud.ingest",
                    "params": {"scope": "today", "title": "Smoke ingest"},
                },
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            assert body["data"]["item"]["scope"] == "today"
            assert body["data"]["classification"]["intent"] == "ingest"
        finally:
            await client.close()

    asyncio.run(run())


def test_http_ingest_parity(app):
    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await _post(
                client,
                "/hud/ingest",
                json={"scope": "today", "title": "HTTP ingest"},
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["route"] == "/hud/ingest"
            assert body["data"]["item"]["internal_id"]
        finally:
            await client.close()

    asyncio.run(run())


def test_mcp_project_default(app):
    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await _post(
                client,
                "/hud/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "hud.project",
                    "params": {"scope": "today", "title": "Plan item"},
                },
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["data"]["intent"] == "project"
            assert "classification" in body["data"]
        finally:
            await client.close()

    asyncio.run(run())


def test_onboarding_ritual_controlled_without_atomic(app, tmp_path, monkeypatch):
    soul = tmp_path / "ritual-soul.md"
    monkeypatch.setenv("HUD_SOUL_MD_PATH", str(soul))

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await _post(
                client,
                "/hud/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "hud.onboarding",
                    "params": {"markdown": _MINIMAL_VALID_SOUL_MD},
                },
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ritual_controlled"
            assert body["data"]["mcp_meta"]["mode"] == "strict_ritual"
            assert "[MCP RITUAL MODE - STRICT PROCEDURE]" in body["data"]["mcp_meta"]["instruction"]
        finally:
            await client.close()

    asyncio.run(run())


def test_onboarding_atomic_write_ok_without_complete(app, tmp_path, monkeypatch):
    soul = tmp_path / "complete-soul.md"
    monkeypatch.setenv("HUD_SOUL_MD_PATH", str(soul))

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await _post(
                client,
                "/hud/mcp",
                json={
                    "jsonrpc": "2.0",
                    "method": "hud.onboarding",
                    "params": {
                        "markdown": _MINIMAL_VALID_SOUL_MD,
                        "roles": [{"slug": "owner", "name": "Owner", "description": "Lead"}],
                        "goals_by_role": {
                            "owner": [{"goal": "Ship v1", "done_definition": "Q1"}]
                        },
                        "primary_role_ref": "owner",
                        "primary_goal_ref": "Ship v1",
                    },
                },
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ok"
            assert "COMPLETE" not in str(body)
            assert "ritual_complete" not in str(body)
        finally:
            await client.close()

    asyncio.run(run())


def test_onboarding_read_without_markdown(app):
    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await _post(
                client,
                "/hud/mcp",
                json={"jsonrpc": "2.0", "method": "hud.onboarding", "params": {}},
            )
            assert resp.status == 200
            body = await resp.json()
            assert "markdown" in body["data"]
            assert body["data"]["onboarding_complete"] is True
        finally:
            await client.close()

    asyncio.run(run())
