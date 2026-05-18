"""JSON-RPC MCP dispatcher for HUD methods."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, Optional

from aiohttp import web

from hud.contracts import (
    HUD_ERROR_HTTP_STATUS,
    HUD_INTENT_BRIEF,
    HUD_INTENT_INGEST,
    HUD_INTENT_MCP,
    HUD_INTENT_ONBOARDING,
    HUD_INTENT_PROJECT,
    HUD_MCP_METHODS,
    HUD_ROUTE_MCP,
    hud_error_payload,
    hud_success_payload,
    parse_hud_scope,
    require_json,
)
from hud.gates import (
    hud_actor,
    hud_onboarding_gate_response_if_blocked,
    hud_push_policy_client_fields,
    hud_push_policy_gate_response_if_blocked,
    hud_resolve_user_id,
    require_hud_admin,
)
from hud.brief_context import build_classification_context
from hud.handlers import (
    hud_classify_project_pair,
    hud_deterministic_items,
    hud_route_meta,
    hud_store_error_response,
    hud_sync_issue_data,
)
from hud.ingest_project import (
    execute_hud_ingest,
    execute_hud_project_approve_reject,
    execute_hud_project_default,
)
from hud.onboarding import hud_onboarding_dispatch_response, hud_soul_md_path
from hud.onboarding_db import hud_onboarding_db_status
from hud.meta import finalize_hud_data
from hud.store import HUDStore
from hud.workers import HUDWorkers

logger = logging.getLogger(__name__)

_MCP_UNGATED_ONBOARDING = frozenset({"hud.onboarding", "hud.brief"})
_MCP_EXEMPT_PUSH_POLICY = frozenset({"hud.onboarding"})


async def handle_mcp(request: web.Request) -> web.Response:
    actor = hud_actor(request)
    admin_error = await require_hud_admin(request)
    if admin_error is not None:
        return admin_error

    store: HUDStore = request.app["hud_store"]
    workers: HUDWorkers = request.app["hud_workers"]
    hub = request.app["hud_adapter_hub"]

    try:
        payload = require_json(await request.text())
        jsonrpc = payload.get("jsonrpc")
        method = payload.get("method")
        if jsonrpc != "2.0":
            raise ValueError("hud.mcp payload missing jsonrpc='2.0'")
        if not isinstance(method, str) or not method.strip():
            raise ValueError("hud.mcp payload missing method")
        method = method.strip()
    except ValueError as exc:
        return web.json_response(
            hud_error_payload(
                str(exc),
                "validation_error",
                "invalid_payload",
                route=HUD_ROUTE_MCP,
                actor=actor,
            ),
            status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
        )

    route_intent = HUD_MCP_METHODS.get(method)
    if route_intent is None:
        return web.json_response(
            hud_error_payload(
                f"Method '{method}' is not implemented",
                "route_error",
                "method_not_found",
                route=HUD_ROUTE_MCP,
                actor=actor,
            ),
            status=HUD_ERROR_HTTP_STATUS["method_not_found"],
        )

    params = payload.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        return web.json_response(
            hud_error_payload(
                "hud.mcp payload 'params' must be an object",
                "validation_error",
                "invalid_payload",
                route=HUD_ROUTE_MCP,
                actor=actor,
            ),
            status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
        )

    user_id = hud_resolve_user_id(request, payload=params)

    if method not in _MCP_UNGATED_ONBOARDING:
        blocked = hud_onboarding_gate_response_if_blocked(
            store=store,
            route=HUD_ROUTE_MCP,
            actor=actor,
            user_id=user_id,
        )
        if blocked is not None:
            return blocked

    if method not in _MCP_EXEMPT_PUSH_POLICY:
        push_blocked = hud_push_policy_gate_response_if_blocked(
            store=store,
            route=HUD_ROUTE_MCP,
            actor=actor,
            user_id=user_id,
        )
        if push_blocked is not None:
            return push_blocked

    route_meta = hud_route_meta(workers, route_intent, method=method, jsonrpc=jsonrpc)

    if route_intent == HUD_INTENT_ONBOARDING:
        return hud_onboarding_dispatch_response(
            store=store,
            actor=actor,
            payload=params,
            route=HUD_ROUTE_MCP,
            route_meta=route_meta,
            request=request,
        )

    if route_intent == HUD_INTENT_BRIEF:
        return await _dispatch_brief(
            request,
            store=store,
            workers=workers,
            actor=actor,
            params=params,
            user_id=user_id,
            route_meta=route_meta,
        )

    if route_intent == HUD_INTENT_INGEST:
        return await execute_hud_ingest(
            request,
            store=store,
            workers=workers,
            hub=hub,
            payload=params,
            route=HUD_ROUTE_MCP,
            actor=actor,
            route_meta=route_meta,
        )

    if route_intent == HUD_INTENT_PROJECT:
        action = str(params.get("action") or params.get("fate") or "project").strip().lower()
        if action not in ("project", "approve", "reject"):
            action = "project"
        if action in ("approve", "reject"):
            return await execute_hud_project_approve_reject(
                request,
                store=store,
                workers=workers,
                hub=hub,
                params=params,
                action=action,
                route=HUD_ROUTE_MCP,
                actor=actor,
                route_meta=hud_route_meta(workers, action, method=method, jsonrpc=jsonrpc),
            )
        return await execute_hud_project_default(
            request,
            store=store,
            workers=workers,
            params=params,
            route=HUD_ROUTE_MCP,
            actor=actor,
            route_meta=route_meta,
        )

    if route_intent == HUD_INTENT_MCP:
        return web.json_response(
            hud_error_payload(
                f"Method '{method}' is not implemented in Phase 1",
                "route_error",
                "not_implemented",
                route=HUD_ROUTE_MCP,
                actor=actor,
            ),
            status=HUD_ERROR_HTTP_STATUS["not_implemented"],
        )

    return web.json_response(
        hud_error_payload(
            f"Method '{method}' is not implemented",
            "route_error",
            "method_not_found",
            route=HUD_ROUTE_MCP,
            actor=actor,
        ),
        status=HUD_ERROR_HTTP_STATUS["method_not_found"],
    )


async def _dispatch_brief(
    request: web.Request,
    *,
    store: HUDStore,
    workers: HUDWorkers,
    actor: Optional[str],
    params: Dict[str, Any],
    user_id: str,
    route_meta: Dict[str, Any],
) -> web.Response:
    scope_source = params.get("scope")
    explicit_scope = scope_source is not None and str(scope_source).strip() != ""
    if explicit_scope:
        try:
            scope = parse_hud_scope(scope_source)
        except ValueError as exc:
            return web.json_response(
                hud_error_payload(
                    str(exc),
                    "validation_error",
                    "invalid_payload",
                    route=HUD_ROUTE_MCP,
                    actor=actor,
                ),
                status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
            )
    else:
        scope = None

    if not explicit_scope:
        try:
            soul_content = hud_soul_md_path().read_text(encoding="utf-8")
        except OSError as exc:
            return hud_store_error_response(route=HUD_ROUTE_MCP, actor=actor, exc=exc)

        classification, projection = hud_classify_project_pair(
            workers, {"intent": HUD_INTENT_BRIEF, **params}, intent=HUD_INTENT_BRIEF
        )
        brief_data = finalize_hud_data(
            build_classification_context(
                store,
                user_id,
                soul_content,
                classification=classification,
                projection=projection,
            ),
            store=store,
            user_id=user_id,
        )
        return web.json_response(
            hud_success_payload(
                HUD_ROUTE_MCP,
                status="ok",
                actor=actor,
                route_meta=route_meta,
                data=brief_data,
            ),
            status=200,
        )

    try:
        queued_items = store.list_items(status="queued", limit=100, offset=0)
        pending_approval_items = store.list_items(
            status="pending_approval", limit=100, offset=0
        )
    except sqlite3.DatabaseError as exc:
        return hud_store_error_response(route=HUD_ROUTE_MCP, actor=actor, exc=exc)
    except Exception as exc:
        return hud_store_error_response(route=HUD_ROUTE_MCP, actor=actor, exc=exc)

    all_items = queued_items + pending_approval_items
    scoped_items = [item for item in all_items if item.get("scope") == scope]
    deterministic_items = hud_deterministic_items(scoped_items)
    brief_issue_data = hud_sync_issue_data(deterministic_items)
    onboarding_context = hud_onboarding_db_status(store, user_id)
    onboarding_needed = onboarding_context["required"]
    has_pending_approval = any(
        item.get("status") == "pending_approval" for item in scoped_items
    )
    has_queued = any(item.get("status") == "queued" for item in scoped_items)
    policy_fields = hud_push_policy_client_fields(store, user_id)
    next_action = (
        "onboard"
        if onboarding_needed
        else "process"
        if has_queued
        else ("review" if has_pending_approval else "wait")
    )
    if policy_fields.get("post_onboarding_push_policy_required"):
        next_action = "choose_push_policy"

    classification, projection = hud_classify_project_pair(
        workers,
        {"intent": HUD_INTENT_BRIEF, "scope": scope, **params},
        intent=HUD_INTENT_BRIEF,
    )

    scoped_data = finalize_hud_data(
        {
            "scope": scope,
            "items": deterministic_items,
            "next_action": next_action,
            "onboarding_needed": onboarding_needed,
            "onboarding_needed_reason": onboarding_context,
            "classification": classification,
            "projection": projection,
            **policy_fields,
            **brief_issue_data,
        },
        store=store,
        user_id=user_id,
    )
    return web.json_response(
        hud_success_payload(
            HUD_ROUTE_MCP,
            status="ok",
            actor=actor,
            route_meta=route_meta,
            data=scoped_data,
        ),
        status=200,
    )
