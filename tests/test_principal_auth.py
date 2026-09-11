import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer, make_mocked_request

from hud.gates import require_hud_principal
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


def _store_app(tmp_path):
    store = HUDStore(str(tmp_path / "hud.db"))
    app = web.Application()
    app["hud_store"] = store
    return store, app


def test_brief_without_header_is_invalid_hud_admin_key(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    app = create_app()

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post("/hud/brief", json={})
            assert resp.status == 401
            body = await resp.json()
            assert body["error"]["code"] == "invalid_hud_admin_key"
            assert body["error"]["type"] == "authentication_error"
        finally:
            await client.close()

    asyncio.run(run())


def test_brief_wrong_admin_key_is_invalid_hud_admin_key(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    app = create_app()

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/brief",
                headers={"X-HUD-Admin-Key": "wrong-admin-key"},
                json={},
            )
            assert resp.status == 401
            body = await resp.json()
            assert body["error"]["code"] == "invalid_hud_admin_key"
            assert body["error"]["type"] == "authentication_error"
        finally:
            await client.close()

    asyncio.run(run())


def test_health_and_openapi_ungated(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    app = create_app()

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            health = await client.get("/health")
            assert health.status == 200
            health_body = await health.json()
            assert health_body["service"] == "AdventedHUD"

            spec = await client.get("/openapi.json")
            assert spec.status == 200
            spec_body = await spec.json()
            assert spec_body["openapi"].startswith("3.")
        finally:
            await client.close()

    asyncio.run(run())


def test_admin_plus_agent_header_is_admin_principal(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    store, app = _store_app(tmp_path)
    minted = store.provision_agent_key("content", _CONTENT_GRANTS)

    async def run():
        req = make_mocked_request(
            "POST",
            "/hud/brief",
            headers={
                "X-HUD-Admin-Key": "test-admin-key",
                "X-HUD-Agent-Key": minted["key"],
            },
            app=app,
        )
        denied = await require_hud_principal(req)
        assert denied is None
        principal = req["hud_principal"]
        assert principal["type"] == "admin"
        assert principal.get("agent_id") != "content"

        junk = make_mocked_request(
            "POST",
            "/hud/brief",
            headers={
                "X-HUD-Admin-Key": "test-admin-key",
                "X-HUD-Agent-Key": "junk-agent-key",
            },
            app=app,
        )
        denied_junk = await require_hud_principal(junk)
        assert denied_junk is None
        assert junk["hud_principal"]["type"] == "admin"

    asyncio.run(run())


def test_admin_plus_agent_header_brief_is_not_401(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    app = create_app()
    store: HUDStore = app["hud_store"]
    minted = store.provision_agent_key("content", _CONTENT_GRANTS)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/brief",
                headers={
                    "X-HUD-Admin-Key": "test-admin-key",
                    "X-HUD-Agent-Key": minted["key"],
                },
                json={},
            )
            assert resp.status != 401
            body = await resp.json()
            assert body.get("error", {}).get("code") != "invalid_hud_admin_key"
        finally:
            await client.close()

    asyncio.run(run())


def test_valid_agent_key_alone_is_agent_principal_never_admin(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    store, app = _store_app(tmp_path)
    minted = store.provision_agent_key("content", _CONTENT_GRANTS)

    async def run():
        req = make_mocked_request(
            "POST",
            "/hud/brief",
            headers={"X-HUD-Agent-Key": minted["key"]},
            app=app,
        )
        denied = await require_hud_principal(req)
        assert denied is None
        principal = req["hud_principal"]
        assert principal["type"] == "agent"
        assert principal["type"] != "admin"
        assert principal.get("agent_id") == "content"
        assert principal.get("allowed_role_refs") == ["content"]
        assert principal.get("allowed_tools") == ["hud.brief", "hud.ingest", "hud.project"]

        both_wrong_admin = make_mocked_request(
            "POST",
            "/hud/brief",
            headers={
                "X-HUD-Admin-Key": "wrong-admin-key",
                "X-HUD-Agent-Key": minted["key"],
            },
            app=app,
        )
        denied_wrong_admin = await require_hud_principal(both_wrong_admin)
        assert denied_wrong_admin is None
        assert both_wrong_admin["hud_principal"]["type"] == "agent"

    asyncio.run(run())


def test_junk_agent_key_alone_is_401(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    store, app = _store_app(tmp_path)
    store.provision_agent_key("content", _CONTENT_GRANTS)

    async def run():
        req = make_mocked_request(
            "POST",
            "/hud/brief",
            headers={"X-HUD-Agent-Key": "not-a-stored-key"},
            app=app,
        )
        denied = await require_hud_principal(req)
        assert denied is not None
        assert denied.status == 401
        body = denied.body
        # aiohttp json_response stores encoded body
        import json

        payload = json.loads(body)
        assert payload["error"]["code"] == "invalid_hud_admin_key"

    asyncio.run(run())
