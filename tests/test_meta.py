import asyncio
import json

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hud.server import create_app
from hud.store import HUDStore

_MINIMAL_VALID_SOUL_MD = """## Roles Matrix
| Role Slug | Role Name | Brief Description |
| --- | --- | --- |
| owner | Owner | Lead |

## Goals Matrix
| Role | Goal | Done |
| --- | --- | --- |
| Owner | Ship v1 | Q1 |
"""


@pytest.fixture(autouse=True)
def _hud_meta_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUD_ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("HUD_REQUIRE_POST_ONBOARDING_PUSH", "0")
    monkeypatch.setenv("HUD_DB_PATH", str(tmp_path / "hud.db"))
    soul = tmp_path / "soul.md"
    soul.write_text(_MINIMAL_VALID_SOUL_MD, encoding="utf-8")
    monkeypatch.setenv("HUD_SOUL_MD_PATH", str(soul))


def _admin_headers():
    return {"X-HUD-Admin-Key": "test-admin-key"}


@pytest.fixture
def app():
    return create_app()


def test_brief_includes_efficiency_mcp_meta_without_onboarding_row(app, tmp_path):
    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/mcp",
                headers=_admin_headers(),
                json={"jsonrpc": "2.0", "method": "hud.brief"},
            )
            assert resp.status == 200
            body = await resp.json()
            meta = body["data"]["mcp_meta"]
            assert meta["mode"] == "efficiency"
            assert "You are inside the MCP" in meta["instruction"]
            store = HUDStore(str(tmp_path / "hud.db"))
            assert store.get_user_onboarding_state("localuser") is None
        finally:
            await client.close()

    asyncio.run(run())


def test_onboarding_atomic_write_has_no_complete_signals(app, tmp_path, monkeypatch):
    soul = tmp_path / "atomic-soul.md"
    monkeypatch.setenv("HUD_SOUL_MD_PATH", str(soul))

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/mcp",
                headers=_admin_headers(),
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
            serialized = json.dumps(body)
            assert "COMPLETE" not in serialized
            assert "ritual_complete" not in serialized
            assert "instruction" not in body["data"] or body["data"].get("instruction") is None
        finally:
            await client.close()

    asyncio.run(run())


def test_onboarding_write_without_atomic_uses_strict_ritual_meta(app, tmp_path, monkeypatch):
    soul = tmp_path / "ritual-soul.md"
    monkeypatch.setenv("HUD_SOUL_MD_PATH", str(soul))

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/mcp",
                headers=_admin_headers(),
                json={
                    "jsonrpc": "2.0",
                    "method": "hud.onboarding",
                    "params": {"markdown": _MINIMAL_VALID_SOUL_MD},
                },
            )
            assert resp.status == 200
            body = await resp.json()
            assert body["status"] == "ritual_controlled"
            meta = body["data"]["mcp_meta"]
            assert meta["mode"] == "strict_ritual"
            assert "You are inside the MCP" in meta["instruction"]
            assert "[MCP RITUAL MODE - STRICT PROCEDURE]" in meta["instruction"]
            assert "instruction" not in body["data"] or body["data"].get("instruction") is None
        finally:
            await client.close()

    asyncio.run(run())
