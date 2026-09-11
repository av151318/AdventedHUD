import asyncio
from datetime import datetime, timedelta, timezone

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

_SOUL = (
    "## My Mission Statement\n\n"
    "Build tools that help people live with intention.\n\n"
    "## Roles Matrix\n| Role Slug | Role Name | Brief Description |\n"
    "| --- | --- | --- |\n"
    "| founder | Founder | Found |\n"
    "| engineer | Engineer | Eng |\n"
    "| friend | Friend | Fr |\n"
    "| content | Content | Content |\n\n"
    "## Goals Matrix\n| Role | Goal | Done |\n"
    "| --- | --- | --- |\n"
    "| Founder | Founder goal | Q1 |\n"
    "| Engineer | Eng goal | Q1 |\n"
    "| Friend | Friend goal | Q1 |\n"
    "| Content | Publish | Q1 |\n"
)


class _FakeGCal:
    def list_calendars(self, payload=None):
        return {
            "calendars": [
                {"id": "primary", "summary": "Primary"},
                {"id": "secret-work-cal", "summary": "Work"},
                {"id": "friend-cal", "summary": "Friends"},
            ]
        }


class _FakeGTasks:
    def list_task_lists(self, payload=None):
        return {
            "task_lists": [
                {"id": "@default", "title": "My Tasks"},
                {"id": "secret-tasklist", "title": "Private"},
            ]
        }


def _env(monkeypatch, tmp_path):
    monkeypatch.setenv("HUD_ADMIN_API_KEY", "test-admin-key")
    monkeypatch.setenv("HUD_REQUIRE_POST_ONBOARDING_PUSH", "0")
    monkeypatch.setenv("HUD_ALLOW_WRITES", "true")
    monkeypatch.setenv("HUD_DB_PATH", str(tmp_path / "hud.db"))
    soul = tmp_path / "soul.md"
    soul.write_text(_SOUL, encoding="utf-8")
    monkeypatch.setenv("HUD_SOUL_MD_PATH", str(soul))


def _onboard(tmp_path) -> HUDStore:
    store = HUDStore(str(tmp_path / "hud.db"))
    store.set_user_onboarding_atomic(
        "localuser",
        roles=[
            {"slug": "founder", "name": "Founder", "description": "Found"},
            {"slug": "engineer", "name": "Engineer", "description": "Eng"},
            {"slug": "friend", "name": "Friend", "description": "Fr"},
            {"slug": "content", "name": "Content", "description": "Content"},
        ],
        goals_by_role={
            "founder": [{"goal": "Founder goal", "done_definition": "Q1"}],
            "engineer": [{"goal": "Eng goal", "done_definition": "Q1"}],
            "friend": [{"goal": "Friend goal", "done_definition": "Q1"}],
            "content": [{"goal": "Publish", "done_definition": "Q1"}],
        },
        primary_role_ref="founder",
        primary_goal_ref="Founder goal",
    )
    store.set_user_push_policy("localuser", external_push=False)
    return store


def _seed_items(store: HUDStore) -> None:
    due = (datetime.now(timezone.utc) + timedelta(days=2)).isoformat()
    for role, title, status, scope in (
        ("content", "Content video", "queued", "today"),
        ("founder", "Founder secret", "queued", "today"),
        ("engineer", "Engineer secret", "queued", "today"),
        ("friend", "Friend secret", "pending_approval", "today"),
        ("content", "Content approved", "approved", "today"),
        ("founder", "Founder approved", "approved", "today"),
    ):
        store.upsert_item(
            {
                "internal_id": f"item-{role}-{status}",
                "status": status,
                "scope": scope,
                "role_ref": role,
                "semantic_type": "todo",
                "priority_class": "high",
                "payload_json": {"title": title, "due": due, "role_ref": role},
            }
        )


def _wire_fake_google(app):
    hub = app["hud_adapter_hub"]
    hub.adapters["gcal"] = _FakeGCal()
    hub.adapters["gtasks"] = _FakeGTasks()


def _mint(app):
    store: HUDStore = app["hud_store"]
    return store.provision_agent_key("content", _CONTENT_GRANTS)


def _agent_headers(key: str):
    return {"X-HUD-Agent-Key": key}


def _admin_headers():
    return {"X-HUD-Admin-Key": "test-admin-key"}


def _role_slugs(data):
    return [str(role.get("slug") or "").lower() for role in (data.get("roles") or [])]


def _goal_keys(data):
    return [str(key).lower() for key in (data.get("goals_by_role") or {})]


def _projection_roles(data):
    roles = []
    for item in data.get("current_projection") or []:
        roles.append(str(item.get("role") or item.get("role_ref") or "").lower())
    return roles


def _item_roles(data):
    roles = []
    for item in data.get("items") or []:
        roles.append(str(item.get("role_ref") or "").lower())
    return roles


def _calendar_ids(data):
    ctx = data.get("google_context") or {}
    return [str(cal.get("id")) for cal in (ctx.get("calendars") or [])]


def _tasklist_ids(data):
    ctx = data.get("google_context") or {}
    return [str(lst.get("id")) for lst in (ctx.get("task_lists") or [])]


def _assert_agent_filtered(data):
    slugs = _role_slugs(data)
    assert slugs == ["content"], slugs
    for key in _goal_keys(data):
        assert key in {"content"}
        assert key not in {"founder", "engineer", "friend"}
    blob = str(data).lower()
    assert "founder secret" not in blob
    assert "engineer secret" not in blob
    assert "friend secret" not in blob
    assert "secret-work-cal" not in blob
    assert "friend-cal" not in blob
    assert "secret-tasklist" not in blob
    for role in _projection_roles(data):
        assert role == "content"
    ctx = data.get("google_context") or {}
    assert _calendar_ids(data) == ["primary"]
    assert _tasklist_ids(data) == ["@default"]
    assert ctx.get("primary_calendar") == "primary"
    assert ctx.get("default_task_list") == "@default"


def _assert_admin_unfiltered(data):
    slugs = set(_role_slugs(data))
    assert {"founder", "engineer", "friend", "content"}.issubset(slugs)
    goal_keys = set(_goal_keys(data))
    assert "founder" in goal_keys
    assert "engineer" in goal_keys
    assert "content" in goal_keys
    proj_roles = set(_projection_roles(data))
    assert "founder" in proj_roles
    assert "content" in proj_roles
    assert "secret-work-cal" in _calendar_ids(data)
    assert "primary" in _calendar_ids(data)
    assert "secret-tasklist" in _tasklist_ids(data)
    assert "@default" in _tasklist_ids(data)


def test_agent_http_brief_filters_roles_items_and_google_context(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    _wire_fake_google(app)
    store: HUDStore = app["hud_store"]
    _seed_items(store)
    minted = _mint(app)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/brief",
                headers=_agent_headers(minted["key"]),
                json={},
            )
            assert resp.status == 200
            body = await resp.json()
            data = body["data"]
            assert data["mode"] == "classification_context"
            _assert_agent_filtered(data)
        finally:
            await client.close()

    asyncio.run(run())


def test_admin_http_brief_keeps_full_classification_context(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    _wire_fake_google(app)
    store: HUDStore = app["hud_store"]
    _seed_items(store)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/brief",
                headers=_admin_headers(),
                json={},
            )
            assert resp.status == 200
            data = (await resp.json())["data"]
            assert data["mode"] == "classification_context"
            _assert_admin_unfiltered(data)
        finally:
            await client.close()

    asyncio.run(run())


def test_agent_http_scoped_brief_hides_other_role_items(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    _wire_fake_google(app)
    store: HUDStore = app["hud_store"]
    _seed_items(store)
    minted = _mint(app)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/brief",
                headers=_agent_headers(minted["key"]),
                json={"scope": "today"},
            )
            assert resp.status == 200
            data = (await resp.json())["data"]
            roles = set(_item_roles(data))
            assert roles == {"content"}
            titles = [
                str((item.get("payload_json") or {}).get("title") or "")
                for item in data.get("items") or []
            ]
            assert "Content video" in titles
            assert "Founder secret" not in titles
            assert "Engineer secret" not in titles
            assert "Friend secret" not in titles
        finally:
            await client.close()

    asyncio.run(run())


def test_admin_http_scoped_brief_keeps_all_role_items(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    _wire_fake_google(app)
    store: HUDStore = app["hud_store"]
    _seed_items(store)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/brief",
                headers=_admin_headers(),
                json={"scope": "today"},
            )
            assert resp.status == 200
            data = (await resp.json())["data"]
            roles = set(_item_roles(data))
            assert {"content", "founder", "engineer", "friend"}.issubset(roles)
        finally:
            await client.close()

    asyncio.run(run())


def test_agent_mcp_brief_filters_like_http(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    _wire_fake_google(app)
    store: HUDStore = app["hud_store"]
    _seed_items(store)
    minted = _mint(app)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/mcp",
                headers=_agent_headers(minted["key"]),
                json={"jsonrpc": "2.0", "method": "hud.brief", "params": {}},
            )
            assert resp.status == 200
            data = (await resp.json())["data"]
            assert data["mode"] == "classification_context"
            _assert_agent_filtered(data)
        finally:
            await client.close()

    asyncio.run(run())


def test_admin_mcp_brief_keeps_full_classification_context(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    _wire_fake_google(app)
    store: HUDStore = app["hud_store"]
    _seed_items(store)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/mcp",
                headers=_admin_headers(),
                json={"jsonrpc": "2.0", "method": "hud.brief", "params": {}},
            )
            assert resp.status == 200
            data = (await resp.json())["data"]
            assert data["mode"] == "classification_context"
            _assert_admin_unfiltered(data)
        finally:
            await client.close()

    asyncio.run(run())


def test_agent_mcp_scoped_brief_hides_other_role_items(monkeypatch, tmp_path):
    _env(monkeypatch, tmp_path)
    _onboard(tmp_path)
    app = create_app()
    store: HUDStore = app["hud_store"]
    _seed_items(store)
    minted = _mint(app)

    async def run():
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            resp = await client.post(
                "/hud/mcp",
                headers=_agent_headers(minted["key"]),
                json={
                    "jsonrpc": "2.0",
                    "method": "hud.brief",
                    "params": {"scope": "today"},
                },
            )
            assert resp.status == 200
            data = (await resp.json())["data"]
            assert set(_item_roles(data)) == {"content"}
        finally:
            await client.close()

    asyncio.run(run())
