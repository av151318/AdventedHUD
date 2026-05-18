import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hud.brief_context import DECISION_MATRIX, extract_mission
from hud.server import create_app
from hud.store import HUDStore

_SOUL_WITH_MISSION = """## My Mission Statement

Build tools that help people live with intention.

## Part 12 Roles
| Role Slug | Role Name | Brief Description |
| --- | --- | --- |
| owner | Owner | Lead |

## Part 13 Goals Per Role
| Role | Goal | Done |
| --- | --- | --- |
| Owner | Ship v1 | Q1 |
"""


@pytest.fixture
def app():
    return create_app()


@pytest.fixture(autouse=True)
def _brief_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUD_ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("HUD_REQUIRE_POST_ONBOARDING_PUSH", "0")
    monkeypatch.setenv("HUD_DB_PATH", str(tmp_path / "hud.db"))
    soul = tmp_path / "soul.md"
    soul.write_text(_SOUL_WITH_MISSION, encoding="utf-8")
    monkeypatch.setenv("HUD_SOUL_MD_PATH", str(soul))


def test_extract_mission_primary_section():
    mission = extract_mission(_SOUL_WITH_MISSION)
    assert mission["source"] == "mission_statement"
    assert "intention" in mission["text"]


def test_extract_mission_vision_fallback():
    content = "## Vision\n\nServe my community.\n\n## Part 12 Roles\n| a | b | c |\n|---|---|---|\n| x | y | z |"
    mission = extract_mission(content)
    assert mission["source"] == "vision_fallback"
    assert "community" in mission["text"]


def test_mcp_brief_default_has_full_matrix(app):
    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/mcp",
                headers={"X-HUD-Admin-Key": "test-admin-key"},
                json={"jsonrpc": "2.0", "method": "hud.brief"},
            )
            assert resp.status == 200
            data = (await resp.json())["data"]
            assert data["mode"] == "classification_context"
            assert data["mission"]["text"]
            assert len(data["decision_matrix"]["quadrants"]) == 4
            assert data["decision_matrix"]["quadrants"][0]["id"] == "Q1"
            assert data["onboarding_state"] == "incomplete"
            assert "roles" in data and "goals_by_role" in data
        finally:
            await client.close()

    asyncio.run(run())


def test_onboarding_state_fully_onboarded_when_db_and_push_set(app, tmp_path):
    store = HUDStore(str(tmp_path / "hud.db"))
    store.set_user_onboarding_atomic(
        "localuser",
        roles=[{"slug": "owner", "name": "Owner", "description": "Lead"}],
        goals_by_role={"owner": [{"goal": "Ship v1", "done_definition": "Q1"}]},
        primary_role_ref="owner",
        primary_goal_ref="Ship v1",
    )
    store.set_user_push_policy("localuser", external_push=False)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/mcp",
                headers={"X-HUD-Admin-Key": "test-admin-key"},
                json={"jsonrpc": "2.0", "method": "hud.brief"},
            )
            body = await resp.json()
            assert body["data"]["onboarding_state"] == "fully_onboarded"
            assert body["data"]["push_policy"]["set"] is True
        finally:
            await client.close()

    asyncio.run(run())


def test_decision_matrix_matches_spec_constants():
    assert DECISION_MATRIX["name"] == "FranklinCovey Time Management Matrix"
    ids = [q["id"] for q in DECISION_MATRIX["quadrants"]]
    assert ids == ["Q1", "Q2", "Q3", "Q4"]
