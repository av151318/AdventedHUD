"""Post-projection retract/update lifecycle: delete GCal/GTasks + Obsidian."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp.test_utils import TestClient, TestServer

from hud.adapters import ObsidianAdapter
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


async def _fake_dispatch(hub, intent, payload):
    """Hub-shaped adapter results keyed by intent (no real network)."""
    if intent == "ingest":  # primary obsidian upsert
        return {
            "status": "ok",
            "intent": intent,
            "adapter": "obsidian",
            "action": "upsert_item",
            "result": {
                "status": "ok",
                "write_status": "created",
                "relative_path": "data/obsidian/AdventedHUD/inbox/note.md",
                "file_path": "/app/data/obsidian/AdventedHUD/inbox/note.md",
            },
        }
    if intent == "gtasks.upsert":
        return {
            "status": "ok",
            "intent": intent,
            "adapter": "gtasks",
            "action": "upsert_item",
            "result": {
                "status": "ok",
                "write_status": "created",
                "external_id": "task-123",
                "google_id": "task-123",
                "tasklist_id": "@default",
            },
        }
    if intent == "gcal.upsert":
        return {
            "status": "ok",
            "intent": intent,
            "adapter": "gcal",
            "action": "upsert_item",
            "result": {
                "status": "ok",
                "write_status": "created",
                "external_id": "evt-1",
                "google_id": "evt-1",
                "calendar_id": "primary",
            },
        }
    if intent in ("obsidian.delete", "gtasks.delete", "gcal.delete"):
        return {
            "status": "ok",
            "intent": intent,
            "adapter": intent.split(".")[0],
            "action": "delete_item",
            "result": {"status": "ok", "write_status": "deleted"},
        }
    return {
        "status": "ok",
        "intent": intent,
        "adapter": None,
        "action": "noop",
        "result": {"status": "ok"},
    }


async def _ingest_approve(client, payload):
    ingest_resp = await client.post("/hud/ingest", headers=_admin_headers(), json=payload)
    assert ingest_resp.status == 200
    ingest_body = await ingest_resp.json()
    ingest_data = ingest_body.get("data") or ingest_body
    item_id = (ingest_data.get("item") or {}).get("internal_id")
    assert item_id
    approve_resp = await client.post(
        "/hud/project",
        headers=_admin_headers(),
        json={"item_id": item_id, "action": "approve", "projection_mode": "live"},
    )
    assert approve_resp.status == 200
    return item_id


def test_reject_non_projected_no_external_delete(app, tmp_path):
    async def run():
        store = _onboard_store(tmp_path)
        store.upsert_item(
            {
                "internal_id": "queue-item-1",
                "actor": "alice",
                "intent": "ingest",
                "scope": "today",
                "status": "queued",
                "google_target": "tasks",
                "payload_json": {"title": "queued task"},
            }
        )
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new_callable=AsyncMock,
            return_value={
                "status": "ok",
                "intent": "noop",
                "adapter": None,
                "action": "noop",
                "result": {"status": "ok"},
            },
        ) as mock_dispatch:
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/hud/project",
                    headers=_admin_headers(),
                    json={"item_id": "queue-item-1", "action": "reject"},
                )
                assert resp.status == 200
                body = await resp.json()
                data = body.get("data") or body
                assert data["item"]["status"] == "rejected"
                delete_calls = [
                    c for c in mock_dispatch.call_args_list if str(c.args[1]).endswith(".delete")
                ]
                assert delete_calls == []
            finally:
                await client.close()

    asyncio.run(run())


def test_approve_then_retract_deletes_external(app, tmp_path):
    async def run():
        store = _onboard_store(tmp_path)
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new_callable=AsyncMock,
            side_effect=_fake_dispatch,
        ) as mock_dispatch:
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                item_id = await _ingest_approve(client, _ingest_payload())

                stored = store.get_item(item_id)
                assert stored["external_id"] == "task-123"
                assert stored["tasklist_id"] == "@default"
                assert stored["relative_path"] == "data/obsidian/AdventedHUD/inbox/note.md"

                retract_resp = await client.post(
                    "/hud/project",
                    headers=_admin_headers(),
                    json={"item_id": item_id, "action": "retract"},
                )
                assert retract_resp.status == 200
                body = await retract_resp.json()
                data = body.get("data") or body
                assert data["item"]["status"] == "retracted"

                delete_calls = [
                    c for c in mock_dispatch.call_args_list if str(c.args[1]).endswith(".delete")
                ]
                intents = [c.args[1] for c in delete_calls]
                assert "gtasks.delete" in intents
                assert "obsidian.delete" in intents

                gtasks_delete = next(c for c in delete_calls if c.args[1] == "gtasks.delete")
                delete_payload = gtasks_delete.args[2]
                assert delete_payload["payload"]["external_id"] == "task-123"
                assert delete_payload["payload"]["tasklist_id"] == "@default"
            finally:
                await client.close()

    asyncio.run(run())


def test_update_patches_existing_event(app, tmp_path):
    async def run():
        store = _onboard_store(tmp_path)
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new_callable=AsyncMock,
            side_effect=_fake_dispatch,
        ) as mock_dispatch:
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                item_id = await _ingest_approve(
                    client,
                    _ingest_payload(
                        google_target="calendar",
                        semantic_type="event",
                        start={"dateTime": "2026-08-08T10:00:00", "timeZone": "America/Los_Angeles"},
                        end={"dateTime": "2026-08-08T11:00:00", "timeZone": "America/Los_Angeles"},
                    ),
                )

                stored = store.get_item(item_id)
                assert stored["external_id"] == "evt-1"
                assert stored["calendar_id"] == "primary"

                update_resp = await client.post(
                    "/hud/project",
                    headers=_admin_headers(),
                    json={
                        "item_id": item_id,
                        "action": "update",
                        "start": {"dateTime": "2026-08-08T14:00:00", "timeZone": "America/Los_Angeles"},
                        "end": {"dateTime": "2026-08-08T15:00:00", "timeZone": "America/Los_Angeles"},
                    },
                )
                assert update_resp.status == 200

                gcal_upserts = [c for c in mock_dispatch.call_args_list if c.args[1] == "gcal.upsert"]
                # one during approve, one during update (no duplicate create)
                assert len(gcal_upserts) == 2
                update_call = gcal_upserts[-1]
                update_payload = update_call.args[2]
                assert update_payload["payload"]["external_id"] == "evt-1"
                assert update_payload["payload"]["calendar_id"] == "primary"
                assert update_payload["payload"]["start"]["dateTime"] == "2026-08-08T14:00:00"
            finally:
                await client.close()

    asyncio.run(run())


def test_reject_on_projected_returns_use_retract(app, tmp_path):
    async def run():
        store = _onboard_store(tmp_path)
        store.upsert_item(
            {
                "internal_id": "projected-item-1",
                "actor": "alice",
                "intent": "ingest",
                "scope": "today",
                "status": "approved",
                "google_target": "tasks",
                "external_id": "task-123",
                "payload_json": {"title": "projected task"},
            }
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/project",
                headers=_admin_headers(),
                json={"item_id": "projected-item-1", "action": "reject"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["status"] == "error"
            assert body["error"]["code"] == "use_retract"
            assert "retract" in body["error"]["message"]
        finally:
            await client.close()

    asyncio.run(run())


def test_retract_missing_external_id_errors(app, tmp_path):
    async def run():
        store = _onboard_store(tmp_path)
        store.upsert_item(
            {
                "internal_id": "no-ext-1",
                "actor": "alice",
                "intent": "ingest",
                "scope": "today",
                "status": "approved",
                "google_target": "tasks",
                "payload_json": {"title": "task without external id"},
            }
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/project",
                headers=_admin_headers(),
                json={"item_id": "no-ext-1", "action": "retract"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["status"] == "error"
            assert body["error"]["code"] == "missing_external_id"
        finally:
            await client.close()

    asyncio.run(run())


def test_retract_non_projected_errors(app, tmp_path):
    async def run():
        store = _onboard_store(tmp_path)
        store.upsert_item(
            {
                "internal_id": "queued-only",
                "actor": "alice",
                "intent": "ingest",
                "scope": "today",
                "status": "queued",
                "google_target": "tasks",
                "payload_json": {"title": "not yet projected"},
            }
        )
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/project",
                headers=_admin_headers(),
                json={"item_id": "queued-only", "action": "retract"},
            )
            assert resp.status == 400
            body = await resp.json()
            assert body["status"] == "error"
            assert body["error"]["code"] == "item_not_projected"
        finally:
            await client.close()

    asyncio.run(run())


def test_obsidian_delete_archives_note(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault"
    (vault_root / "inbox").mkdir(parents=True)
    note = vault_root / "inbox" / "note.md"
    note.write_text("# Hello", encoding="utf-8")
    monkeypatch.setenv("HUD_OBSIDIAN_VAULT_ROOT", str(vault_root))

    adapter = ObsidianAdapter(allow_writes=True)

    async def run():
        result = await adapter.delete_item({"relative_path": "inbox/note.md"})
        assert result["status"] == "ok"
        assert result["write_status"] == "archived"
        assert not note.exists()
        archive = vault_root / "_trash" / "inbox" / "note.md"
        assert archive.exists()
        assert archive.read_text(encoding="utf-8") == "# Hello"

    asyncio.run(run())


def test_obsidian_delete_already_gone(tmp_path, monkeypatch):
    vault_root = tmp_path / "vault2"
    (vault_root / "inbox").mkdir(parents=True)
    monkeypatch.setenv("HUD_OBSIDIAN_VAULT_ROOT", str(vault_root))

    adapter = ObsidianAdapter(allow_writes=True)

    async def run():
        result = await adapter.delete_item({"relative_path": "inbox/missing.md"})
        assert result["status"] == "ok"
        assert result["write_status"] == "already_gone"

    asyncio.run(run())


def test_store_retract_transition(tmp_path):
    store = HUDStore(str(tmp_path / "hud.db"))
    store.upsert_item(
        {
            "internal_id": "r-1",
            "status": "approved",
            "intent": "ingest",
            "scope": "today",
            "payload_json": {},
        }
    )
    assert store.transition_status("r-1", "retracted")
    assert store.get_item("r-1")["status"] == "retracted"
    with pytest.raises(ValueError):
        store.transition_status("r-1", "approved")
