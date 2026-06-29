"""Google projection lifecycle: ingest defers, approve dispatches, Pattern B auto-push."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hud.contracts import HUD_PROJECTION_MODE_LIVE
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
def _hud_env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUD_ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("HUD_REQUIRE_POST_ONBOARDING_PUSH", "0")
    monkeypatch.setenv("HUD_ALLOW_WRITES", "true")
    monkeypatch.setenv("HUD_ALLOW_GOOGLE_WRITES", "true")
    monkeypatch.setenv("HUD_DB_PATH", str(tmp_path / "hud.db"))
    soul = tmp_path / "soul.md"
    soul.write_text(_MINIMAL_VALID_SOUL_MD, encoding="utf-8")
    monkeypatch.setenv("HUD_SOUL_MD_PATH", str(soul))


def _admin_headers():
    return {"X-HUD-Admin-Key": "test-admin-key"}


def _onboard_store(tmp_path) -> HUDStore:
    store = HUDStore(str(tmp_path / "hud.db"))
    store.set_user_onboarding_atomic(
        "localuser",
        roles=[{"slug": "owner", "name": "Owner", "description": "Lead"}],
        goals_by_role={"owner": [{"goal": "Ship v1", "done_definition": "Q1"}]},
        primary_role_ref="owner",
        primary_goal_ref="Ship v1",
        requires_approval=True,
    )
    store.set_user_projection_mode("localuser", HUD_PROJECTION_MODE_LIVE)
    return store


@pytest.fixture
def app(tmp_path):
    _onboard_store(tmp_path)
    return create_app()


def _ingest_payload(**overrides):
    base = {
        "title": "Test task",
        "content": "Do the thing",
        "role_ref": "owner",
        "goal_ref": "Ship v1",
        "priority_class": "medium",
        "semantic_type": "todo",
        "google_target": "tasks",
        "projection_mode": "live",
    }
    base.update(overrides)
    return base


def test_ingest_returns_deferred_google_projection(app):
    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/ingest",
                headers=_admin_headers(),
                json=_ingest_payload(),
            )
            assert resp.status == 200
            body = await resp.json()
            data = body.get("data") or body
            assert "google_projection" in data
            gp = data["google_projection"]
            assert gp is not None
            assert gp.get("status") == "deferred"
            assert gp.get("reason") == "awaiting_approval"
            assert gp.get("google_target") == "tasks"
            item = data.get("item") or {}
            assert item.get("status") in ("pending_approval", "queued")
        finally:
            await client.close()

    asyncio.run(run())


def test_approve_triggers_gtasks_dispatch(app, tmp_path):
    async def run():
        store = _onboard_store(tmp_path)
        mock_result = {
            "status": "ok",
            "intent": "gtasks.upsert",
            "adapter": "gtasks",
            "action": "upsert_item",
            "result": {"status": "ok", "dry_run": True},
        }
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_dispatch:
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                ingest_resp = await client.post(
                    "/hud/ingest",
                    headers=_admin_headers(),
                    json=_ingest_payload(),
                )
                ingest_body = await ingest_resp.json()
                ingest_data = ingest_body.get("data") or ingest_body
                item_id = (ingest_data.get("item") or {}).get("internal_id")
                assert item_id

                approve_resp = await client.post(
                    "/hud/project",
                    headers=_admin_headers(),
                    json={
                        "item_id": item_id,
                        "action": "approve",
                        "projection_mode": "live",
                    },
                )
                assert approve_resp.status == 200
                approve_body = await approve_resp.json()
                approve_data = approve_body.get("data") or approve_body
                assert "google_projection" in approve_data
                calls = [c.args[1] for c in mock_dispatch.call_args_list if c.args]
                assert "gtasks.upsert" in calls
            finally:
                await client.close()

    asyncio.run(run())


def test_pattern_b_push_policy_auto_approves_and_dispatches(app, tmp_path):
    async def run():
        store = _onboard_store(tmp_path)
        store.set_user_push_policy("localuser", external_push=True)
        store.set_user_onboarding_state("localuser", requires_approval=False)

        mock_result = {
            "status": "ok",
            "intent": "gtasks.upsert",
            "adapter": "gtasks",
            "action": "upsert_item",
            "result": {"status": "ok"},
        }
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new_callable=AsyncMock,
            return_value=mock_result,
        ) as mock_dispatch:
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/hud/ingest",
                    headers=_admin_headers(),
                    json=_ingest_payload(),
                )
                assert resp.status == 200
                body = await resp.json()
                data = body.get("data") or body
                item = data.get("item") or {}
                assert item.get("status") == "approved"
                gp = data.get("google_projection")
                assert gp is not None
                assert gp.get("status") != "deferred"
                calls = [c.args[1] for c in mock_dispatch.call_args_list if c.args]
                assert "gtasks.upsert" in calls
            finally:
                await client.close()

    asyncio.run(run())


# Live integration (manual): curl -X POST http://127.0.0.1:8200/hud/ingest ...
# with HUD_ALLOW_GOOGLE_WRITES=true and approved OAuth token; then hud.project approve.
