import asyncio
import json
from unittest.mock import AsyncMock, patch

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

_MINIMAL_SOUL = (
    "## Roles Matrix\n| Role Slug | Role Name | Brief Description |\n"
    "| --- | --- | --- |\n| owner | Owner | Lead |\n| content | Content | Content |\n\n"
    "## Goals Matrix\n| Role | Goal | Done |\n"
    "| --- | --- | --- |\n| Owner | Ship v1 | Q1 |\n| Content | Publish | Q1 |\n"
)


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUD_ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("HUD_REQUIRE_POST_ONBOARDING_PUSH", "0")
    monkeypatch.setenv("HUD_ALLOW_WRITES", "true")
    monkeypatch.setenv("HUD_ALLOW_GOOGLE_WRITES", "true")
    monkeypatch.setenv("HUD_DB_PATH", str(tmp_path / "hud.db"))
    soul = tmp_path / "soul.md"
    soul.write_text(_MINIMAL_SOUL, encoding="utf-8")
    monkeypatch.setenv("HUD_SOUL_MD_PATH", str(soul))


def _onboard(tmp_path, *, live_google: bool = False) -> HUDStore:
    store = HUDStore(str(tmp_path / "hud.db"))
    store.set_user_onboarding_atomic(
        "localuser",
        roles=[
            {"slug": "owner", "name": "Owner", "description": "Lead"},
            {"slug": "content", "name": "Content", "description": "Content"},
        ],
        goals_by_role={
            "owner": [{"goal": "Ship v1", "done_definition": "Q1"}],
            "content": [{"goal": "Publish", "done_definition": "Q1"}],
        },
        primary_role_ref="owner",
        primary_goal_ref="Ship v1",
    )
    if live_google:
        store.set_user_push_policy("localuser", external_push=True)
        store.set_user_projection_mode("localuser", "live")
    return store


def _mint_content_agent(app):
    store: HUDStore = app["hud_store"]
    return store.provision_agent_key("content", _CONTENT_GRANTS)


def _agent_headers(key: str):
    return {"X-HUD-Agent-Key": key}


def _admin_headers():
    return {"X-HUD-Admin-Key": "test-admin-key"}


def _assert_forbidden(body):
    assert body["error"]["type"] == "authorization_error"
    assert body["error"]["code"] == "hud_agent_forbidden"


def _assert_no_oauth_tokens(obj):
    blob = json.dumps(obj)
    assert "access_token" not in blob
    assert "refresh_token" not in blob


async def _fake_google_dispatch(hub, intent, payload):
    """Mock Google/Obsidian adapters. Never live-write."""
    if intent == "ingest":
        return {
            "status": "ok",
            "intent": intent,
            "adapter": "obsidian",
            "action": "upsert_item",
            "result": {"status": "ok", "write_status": "created"},
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
                "external_id": "task-acl",
                "google_id": "task-acl",
                "tasklist_id": payload.get("tasklist_id") or "@default",
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
                "external_id": "evt-acl",
                "google_id": "evt-acl",
                "calendar_id": payload.get("calendar_id") or "primary",
            },
        }
    return {
        "status": "ok",
        "intent": intent,
        "adapter": "mock",
        "action": "noop",
        "result": {"status": "ok"},
    }


def test_agent_onboarding_http_is_403(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    minted = _mint_content_agent(app)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for path, method in (
                ("/hud/onboarding/read", "get"),
                ("/hud/onboarding/soul", "get"),
                ("/hud/onboarding/write_soul", "post"),
                ("/hud/onboarding/soul", "post"),
                ("/hud/onboarding/set_atomic", "post"),
                ("/hud/onboarding/set_push", "post"),
            ):
                if method == "get":
                    resp = await client.get(path, headers=_agent_headers(minted["key"]))
                else:
                    resp = await client.post(
                        path,
                        headers=_agent_headers(minted["key"]),
                        json={"markdown": "# no"},
                    )
                assert resp.status == 403, path
                body = await resp.json()
                _assert_forbidden(body)
                _assert_no_oauth_tokens(body)
        finally:
            await client.close()

    asyncio.run(run())


def test_agent_onboarding_mcp_is_403(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    minted = _mint_content_agent(app)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            for method in (
                "hud.onboarding",
                "hud.onboarding.read",
                "hud.onboarding.write_soul",
                "hud.onboarding.set_atomic",
                "hud.onboarding.set_push",
            ):
                resp = await client.post(
                    "/hud/mcp",
                    headers=_agent_headers(minted["key"]),
                    json={"jsonrpc": "2.0", "method": method, "params": {}},
                )
                assert resp.status == 403, method
                body = await resp.json()
                _assert_forbidden(body)
        finally:
            await client.close()

    asyncio.run(run())


def test_agent_disallowed_tool_sync_status_is_403(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    minted = _mint_content_agent(app)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/sync_status",
                headers=_agent_headers(minted["key"]),
                json={},
            )
            assert resp.status == 403
            body = await resp.json()
            _assert_forbidden(body)
        finally:
            await client.close()

    asyncio.run(run())


def test_agent_ingest_founder_role_is_403(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    minted = _mint_content_agent(app)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/ingest",
                headers=_agent_headers(minted["key"]),
                json={
                    "scope": "today",
                    "title": "Founder leak",
                    "role_ref": "founder",
                    "google_target": "obsidian",
                },
            )
            assert resp.status == 403
            body = await resp.json()
            _assert_forbidden(body)
        finally:
            await client.close()

    asyncio.run(run())


def test_agent_ingest_content_role_not_403_for_role(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    minted = _mint_content_agent(app)

    async def run():
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new=AsyncMock(side_effect=_fake_google_dispatch),
        ):
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/hud/ingest",
                    headers=_agent_headers(minted["key"]),
                    json={
                        "scope": "today",
                        "title": "Content ingest",
                        "role_ref": "content",
                        "google_target": "obsidian",
                    },
                )
                assert resp.status != 403
                body = await resp.json()
                assert body.get("error", {}).get("code") != "hud_agent_forbidden"
                _assert_no_oauth_tokens(body)
            finally:
                await client.close()

    asyncio.run(run())


def test_agent_project_update_is_same_tool_not_403_for_tool(monkeypatch, tmp_path):
    """action=update is hud.project, not a separate tool."""
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    minted = _mint_content_agent(app)
    store: HUDStore = app["hud_store"]
    store.upsert_item(
        {
            "internal_id": "acl-update-1",
            "actor": "content",
            "intent": "ingest",
            "scope": "today",
            "status": "approved",
            "payload_json": {"title": "Existing", "role_ref": "content"},
            "role_ref": "content",
            "google_target": "obsidian",
        }
    )

    async def run():
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new=AsyncMock(side_effect=_fake_google_dispatch),
        ):
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/hud/project",
                    headers=_agent_headers(minted["key"]),
                    json={
                        "action": "update",
                        "item_id": "acl-update-1",
                        "title": "Updated title",
                        "role_ref": "content",
                    },
                )
                assert resp.status != 403
                body = await resp.json()
                assert body.get("error", {}).get("code") != "hud_agent_forbidden"
            finally:
                await client.close()

    asyncio.run(run())


def test_agent_non_grant_google_target_is_403(monkeypatch, tmp_path):
    """Policy: 403 authorization_error (hud_agent_forbidden), not silent coerce."""
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    minted = _mint_content_agent(app)

    async def run():
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new=AsyncMock(side_effect=_fake_google_dispatch),
        ):
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/hud/ingest",
                    headers=_agent_headers(minted["key"]),
                    json={
                        "scope": "today",
                        "title": "Drive leak",
                        "role_ref": "content",
                        "google_target": "drive",
                    },
                )
                assert resp.status == 403
                body = await resp.json()
                _assert_forbidden(body)
            finally:
                await client.close()

    asyncio.run(run())


def test_agent_non_grant_calendar_id_is_403(monkeypatch, tmp_path):
    """Policy: 403 when calendar_id is outside grants. Not coerced."""
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    minted = _mint_content_agent(app)

    async def run():
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new=AsyncMock(side_effect=_fake_google_dispatch),
        ):
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/hud/ingest",
                    headers=_agent_headers(minted["key"]),
                    json={
                        "scope": "today",
                        "title": "Wrong calendar",
                        "role_ref": "content",
                        "google_target": "calendar",
                        "calendar_id": "other-calendar",
                    },
                )
                assert resp.status == 403
                body = await resp.json()
                _assert_forbidden(body)
                _assert_no_oauth_tokens(body)
            finally:
                await client.close()

    asyncio.run(run())


def test_agent_non_grant_tasklist_id_is_403(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    minted = _mint_content_agent(app)

    async def run():
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new=AsyncMock(side_effect=_fake_google_dispatch),
        ):
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/hud/ingest",
                    headers=_agent_headers(minted["key"]),
                    json={
                        "scope": "today",
                        "title": "Wrong tasklist",
                        "role_ref": "content",
                        "google_target": "tasks",
                        "tasklist_id": "other-list",
                    },
                )
                assert resp.status == 403
                body = await resp.json()
                _assert_forbidden(body)
            finally:
                await client.close()

    asyncio.run(run())


def test_agent_granted_calendar_write_forced_to_primary(monkeypatch, tmp_path):
    """When calendar_id is omitted, writes are forced to grant calendar_id=primary."""
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path, live_google=True)
    app = create_app()
    minted = _mint_content_agent(app)
    seen = []

    async def capturing_dispatch(hub, intent, payload):
        seen.append((intent, dict(payload)))
        return await _fake_google_dispatch(hub, intent, payload)

    async def run():
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new=AsyncMock(side_effect=capturing_dispatch),
        ):
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/hud/ingest",
                    headers=_agent_headers(minted["key"]),
                    json={
                        "scope": "today",
                        "title": "Primary calendar",
                        "role_ref": "content",
                        "google_target": "calendar",
                        "projection_mode": "live",
                    },
                )
                assert resp.status == 200
                body = await resp.json()
                _assert_no_oauth_tokens(body)
                gcal = [p for intent, p in seen if intent == "gcal.upsert"]
                assert gcal, "expected mocked gcal.upsert"
                nested = gcal[0].get("payload") if isinstance(gcal[0].get("payload"), dict) else {}
                used = gcal[0].get("calendar_id") or nested.get("calendar_id")
                assert used == "primary"
            finally:
                await client.close()

    asyncio.run(run())


def test_agent_granted_tasklist_write_forced_to_default(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path, live_google=True)
    app = create_app()
    minted = _mint_content_agent(app)
    seen = []

    async def capturing_dispatch(hub, intent, payload):
        seen.append((intent, dict(payload)))
        return await _fake_google_dispatch(hub, intent, payload)

    async def run():
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new=AsyncMock(side_effect=capturing_dispatch),
        ):
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/hud/ingest",
                    headers=_agent_headers(minted["key"]),
                    json={
                        "scope": "today",
                        "title": "Default tasks",
                        "role_ref": "content",
                        "google_target": "tasks",
                        "projection_mode": "live",
                    },
                )
                assert resp.status == 200
                body = await resp.json()
                _assert_no_oauth_tokens(body)
                gtasks = [p for intent, p in seen if intent == "gtasks.upsert"]
                assert gtasks, "expected mocked gtasks.upsert"
                nested = gtasks[0].get("payload") if isinstance(gtasks[0].get("payload"), dict) else {}
                used = gtasks[0].get("tasklist_id") or nested.get("tasklist_id")
                assert used == "@default"
            finally:
                await client.close()

    asyncio.run(run())


def test_admin_onboarding_and_ingest_still_allowed(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    _mint_content_agent(app)

    async def run():
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new=AsyncMock(side_effect=_fake_google_dispatch),
        ):
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                read = await client.get(
                    "/hud/onboarding/read", headers=_admin_headers()
                )
                assert read.status == 200

                ingest = await client.post(
                    "/hud/ingest",
                    headers=_admin_headers(),
                    json={
                        "scope": "today",
                        "title": "Admin founder ingest",
                        "role_ref": "founder",
                        "google_target": "calendar",
                        "calendar_id": "other-calendar",
                    },
                )
                assert ingest.status == 200
                body = await ingest.json()
                _assert_no_oauth_tokens(body)

                project = await client.post(
                    "/hud/project",
                    headers=_admin_headers(),
                    json={"action": "project", "role_ref": "founder"},
                )
                assert project.status == 200
            finally:
                await client.close()

    asyncio.run(run())

def test_agent_project_actions_on_stored_founder_item_are_403(monkeypatch, tmp_path):
    """Guessing a founder item_id must 403 even if the body claims content."""
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    minted = _mint_content_agent(app)
    store: HUDStore = app["hud_store"]
    cases = (
        ("approve", "queued", "acl-founder-approve"),
        ("reject", "queued", "acl-founder-reject"),
        ("retract", "approved", "acl-founder-retract"),
        ("update", "approved", "acl-founder-update"),
    )
    for _action, status, item_id in cases:
        store.upsert_item(
            {
                "internal_id": item_id,
                "actor": "admin",
                "intent": "ingest",
                "scope": "today",
                "status": status,
                "payload_json": {"title": "Founder secret", "role_ref": "founder"},
                "role_ref": "founder",
                "google_target": "obsidian",
            }
        )

    async def run():
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new=AsyncMock(side_effect=_fake_google_dispatch),
        ):
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                for action, _status, item_id in cases:
                    resp = await client.post(
                        "/hud/project",
                        headers=_agent_headers(minted["key"]),
                        json={
                            "action": action,
                            "item_id": item_id,
                            "title": "Hijack",
                            "role_ref": "content",
                        },
                    )
                    body = await resp.json()
                    assert resp.status == 403, (action, resp.status, body)
                    _assert_forbidden(body)
                    _assert_no_oauth_tokens(body)
            finally:
                await client.close()

    asyncio.run(run())


def test_agent_project_update_stored_content_item_not_403_for_role(monkeypatch, tmp_path):
    """Stored content role_ref is in grant; omitting role_ref in the body is not 403."""
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    minted = _mint_content_agent(app)
    store: HUDStore = app["hud_store"]
    store.upsert_item(
        {
            "internal_id": "acl-content-stored-1",
            "actor": "content",
            "intent": "ingest",
            "scope": "today",
            "status": "approved",
            "payload_json": {"title": "Content item", "role_ref": "content"},
            "role_ref": "content",
            "google_target": "obsidian",
        }
    )

    async def run():
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new=AsyncMock(side_effect=_fake_google_dispatch),
        ):
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/hud/project",
                    headers=_agent_headers(minted["key"]),
                    json={
                        "action": "update",
                        "item_id": "acl-content-stored-1",
                        "title": "Updated content title",
                    },
                )
                body = await resp.json()
                assert resp.status != 403, body
                assert body.get("error", {}).get("code") != "hud_agent_forbidden"
            finally:
                await client.close()

    asyncio.run(run())


def test_agent_project_update_stored_out_of_grant_calendar_is_403(monkeypatch, tmp_path):
    """Stored calendar_id outside grants must 403 even when the body omits it."""
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    minted = _mint_content_agent(app)
    store: HUDStore = app["hud_store"]
    store.upsert_item(
        {
            "internal_id": "acl-other-cal-1",
            "actor": "content",
            "intent": "ingest",
            "scope": "today",
            "status": "approved",
            "payload_json": {"title": "Other cal", "role_ref": "content"},
            "role_ref": "content",
            "google_target": "calendar",
            "calendar_id": "other-calendar",
        }
    )

    async def run():
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new=AsyncMock(side_effect=_fake_google_dispatch),
        ):
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/hud/project",
                    headers=_agent_headers(minted["key"]),
                    json={
                        "action": "update",
                        "item_id": "acl-other-cal-1",
                        "title": "Try other calendar",
                        "role_ref": "content",
                    },
                )
                body = await resp.json()
                assert resp.status == 403, body
                _assert_forbidden(body)
                _assert_no_oauth_tokens(body)
            finally:
                await client.close()

    asyncio.run(run())


def test_admin_project_update_stored_founder_item_still_200(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    _mint_content_agent(app)
    store: HUDStore = app["hud_store"]
    store.upsert_item(
        {
            "internal_id": "acl-admin-founder-1",
            "actor": "admin",
            "intent": "ingest",
            "scope": "today",
            "status": "approved",
            "payload_json": {"title": "Founder admin", "role_ref": "founder"},
            "role_ref": "founder",
            "google_target": "obsidian",
        }
    )

    async def run():
        with patch(
            "hud.ingest_project.hud_dispatch_projection",
            new=AsyncMock(side_effect=_fake_google_dispatch),
        ):
            client = TestClient(TestServer(app))
            await client.start_server()
            try:
                resp = await client.post(
                    "/hud/project",
                    headers=_admin_headers(),
                    json={
                        "action": "update",
                        "item_id": "acl-admin-founder-1",
                        "title": "Admin may update founder",
                    },
                )
                body = await resp.json()
                assert resp.status == 200, body
            finally:
                await client.close()

    asyncio.run(run())
