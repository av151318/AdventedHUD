import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hud.onboarding_db import is_user_onboarding_atomic_complete, validate_atomic_payload
from hud.server import create_app
from hud.store import HUDStore

_MINIMAL_VALID_SOUL_MD = """## My Mission Statement

Build with intention.

## Roles Matrix
| Role Slug | Role Name | Brief Description |
| --- | --- | --- |
| owner | Owner | Lead |

## Goals Matrix
| Role | Goal | Done |
| --- | --- | --- |
| Owner | Ship v1 | Q1 |
"""


@pytest.fixture(autouse=True)
def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUD_ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("HUD_REQUIRE_POST_ONBOARDING_PUSH", "0")
    monkeypatch.setenv("HUD_DB_PATH", str(tmp_path / "hud.db"))
    soul = tmp_path / "soul.md"
    soul.write_text(_MINIMAL_VALID_SOUL_MD, encoding="utf-8")
    monkeypatch.setenv("HUD_SOUL_MD_PATH", str(soul))


@pytest.fixture
def app():
    return create_app()


def test_validate_atomic_payload_requires_roles():
    issues = validate_atomic_payload([], {}, "owner", "goal")
    assert "roles_empty" in issues


def test_set_user_onboarding_atomic_persists_json(tmp_path):
    store = HUDStore(str(tmp_path / "hud.db"))
    store.set_user_onboarding_atomic(
        "u1",
        roles=[{"slug": "owner", "name": "Owner", "description": "Lead"}],
        goals_by_role={"owner": [{"goal": "Ship", "done_definition": "Done"}]},
        primary_role_ref="owner",
        primary_goal_ref="Ship",
    )
    assert is_user_onboarding_atomic_complete(store, "u1")
    atomic = store.get_user_onboarding_atomic("u1")
    assert atomic is not None
    assert len(atomic["roles"]) == 1


def test_profile_edit_without_strict_ritual(app, tmp_path):
    store = HUDStore(str(tmp_path / "hud.db"))
    store.set_user_onboarding_atomic(
        "localuser",
        roles=[{"slug": "owner", "name": "Owner", "description": "Lead"}],
        goals_by_role={"owner": [{"goal": "Ship v1", "done_definition": "Q1"}]},
        primary_role_ref="owner",
        primary_goal_ref="Ship v1",
    )

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/mcp",
                headers={"X-HUD-Admin-Key": "test-admin-key"},
                json={
                    "jsonrpc": "2.0",
                    "method": "hud.onboarding",
                    "params": {"markdown": _MINIMAL_VALID_SOUL_MD, "profile_edit": True},
                },
            )
            body = await resp.json()
            assert body["status"] == "ok"
            assert body["data"].get("profile_edit") is True
            meta = body["data"].get("mcp_meta")
            assert meta is None or meta.get("mode") != "strict_ritual"
            assert "COMPLETE" not in str(body)
        finally:
            await client.close()

    asyncio.run(run())


def test_ingest_blocked_without_atomic_db(app):
    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/mcp",
                headers={"X-HUD-Admin-Key": "test-admin-key"},
                json={
                    "jsonrpc": "2.0",
                    "method": "hud.ingest",
                    "params": {"scope": "today", "title": "blocked"},
                },
            )
            assert resp.status == 409
            body = await resp.json()
            assert body["data"]["mcp_meta"]["mode"] == "efficiency"
        finally:
            await client.close()

    asyncio.run(run())
