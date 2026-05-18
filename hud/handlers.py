"""HTTP handlers for HUD sync status, brief, and shared helpers."""

from __future__ import annotations

import logging
import sqlite3
from typing import Any, Dict, List, Mapping, Optional

from aiohttp import web

from hud.adapters import HUDAdapterHub
from hud.contracts import (
    HUD_ERROR_HTTP_STATUS,
    HUD_INTENT_BRIEF,
    HUD_ROUTE_BRIEF,
    hud_error_payload,
    hud_success_payload,
    normalize_projection_mode,
    parse_hud_scope,
    require_json,
)
from hud.gates import (
    hud_actor,
    hud_effective_projection_mode,
    hud_onboarding_gate_response_if_blocked,
    hud_parse_explicit_bool,
    hud_projection_mode_client_fields,
    hud_push_policy_client_fields,
    hud_push_policy_gate_response_if_blocked,
    hud_resolve_user_id,
    require_hud_admin,
)
from hud.brief_context import build_classification_context
from hud.onboarding import hud_soul_md_path
from hud.onboarding_db import hud_onboarding_db_status
from hud.meta import finalize_hud_data
from hud.store import HUDStore
from hud.sync import build_brief_sync_report, build_sync_status_payload
from hud.workers import HUDWorkers

logger = logging.getLogger(__name__)

_HUD_TERMINAL_STATUSES = frozenset({"approved", "rejected", "failed", "duplicate", "synced"})


def hud_item_sort_key(item: Dict[str, Any]) -> tuple:
    try:
        queue_rank = int(item.get("queue_rank") or 0)
    except (TypeError, ValueError):
        queue_rank = 0
    created_at = item.get("created_at")
    internal_id = item.get("internal_id")
    return (
        queue_rank,
        str(created_at) if created_at is not None else "",
        str(internal_id) if internal_id is not None else "",
    )


def hud_deterministic_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return sorted(items, key=hud_item_sort_key)


def hud_status_data(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    sync_payload = build_sync_status_payload(items)
    brief_report = build_brief_sync_report(items)
    data: Dict[str, Any] = {
        "summary": sync_payload["summary"],
        "items": items,
    }
    if brief_report["stale"]:
        data["stale"] = brief_report["stale"]
    if brief_report["failed"]:
        data["failed"] = brief_report["failed"]
    if brief_report["pending_approval"]:
        data["pending_approval"] = brief_report["pending_approval"]
    return data


def hud_sync_issue_data(items: List[Dict[str, Any]]) -> Dict[str, Any]:
    brief_report = build_brief_sync_report(items)
    data: Dict[str, Any] = {}
    if brief_report["stale"]:
        data["stale"] = brief_report["stale"]
    if brief_report["failed"]:
        data["failed"] = brief_report["failed"]
    if brief_report["pending_approval"]:
        data["pending_approval"] = brief_report["pending_approval"]
    return data


def hud_store_error_response(
    *,
    route: str,
    actor: Optional[str],
    exc: Exception,
) -> web.Response:
    logger.error("HUD store failure for route=%s error=%s", route, exc, exc_info=True)
    return web.json_response(
        hud_error_payload(
            "HUD store operation failed",
            "internal_error",
            "internal_error",
            route=route,
            actor=actor,
        ),
        status=500,
    )


def hud_route_meta(
    workers: HUDWorkers,
    intent: str,
    *,
    method: Optional[str] = None,
    jsonrpc: Optional[str] = None,
) -> Dict[str, Any]:
    try:
        operation = workers.classify({"intent": intent}).get("action", "noop")
    except Exception:
        operation = "noop"
    route_meta: Dict[str, Any] = {
        "intent": intent,
        "mapped_intent": intent,
        "operation": operation,
    }
    if method is not None:
        route_meta["method"] = method
    if jsonrpc is not None:
        route_meta["jsonrpc"] = jsonrpc
    return route_meta


def hud_worker_input_payload(
    payload: Mapping[str, Any],
    *,
    intent: str,
    status: Optional[str] = None,
) -> Dict[str, Any]:
    normalized = dict(payload)
    normalized.pop("onboarding_needed_reason", None)
    normalized.setdefault("intent", intent)
    if status is not None and not normalized.get("status"):
        normalized["status"] = status
    return normalized


def hud_classify_project_pair(
    workers: HUDWorkers,
    payload: Mapping[str, Any],
    *,
    intent: str,
    status: Optional[str] = None,
    projection_mode: Optional[str] = None,
):
    worker_payload = hud_worker_input_payload(payload, intent=intent, status=status)
    normalized_mode = normalize_projection_mode(projection_mode)
    if normalized_mode is not None:
        worker_payload["projection_mode"] = normalized_mode
    classification = workers.classify(worker_payload)
    projection = workers.project(worker_payload)
    if "onboarding_needed" in worker_payload:
        onboarding_needed = hud_parse_explicit_bool(worker_payload.get("onboarding_needed"))
        if onboarding_needed is not None:
            classification = dict(classification)
            projection = dict(projection)
            classification["onboarding_needed"] = onboarding_needed
            projection["onboarding_needed"] = onboarding_needed
    return classification, projection


async def hud_adapter_sync_previews(hub: HUDAdapterHub) -> Dict[str, Any]:
    previews: Dict[str, Any] = {}
    previews["roles"] = await hub.dispatch("sync", {"kind": "roles"})
    previews["calendar"] = await hub.dispatch("calendar.sync", {"kind": "events"})
    previews["tasks"] = await hub.dispatch("gtasks.sync", {"kind": "tasks"})
    return previews


async def handle_sync_status(request: web.Request) -> web.Response:
    actor = hud_actor(request)
    admin_error = await require_hud_admin(request)
    if admin_error is not None:
        return admin_error

    route = str(request.path)
    store: HUDStore = request.app["hud_store"]
    hub: HUDAdapterHub = request.app["hud_adapter_hub"]

    status_value = request.query.get("status")
    if status_value is not None:
        status_value = status_value.strip() or None

    raw_limit = request.query.get("limit", "100")
    raw_offset = request.query.get("offset", "0")

    try:
        limit = int(raw_limit)
        if limit < 0:
            raise ValueError("hud.status query 'limit' must be >= 0")
    except (TypeError, ValueError) as exc:
        return web.json_response(
            hud_error_payload(
                str(exc),
                "validation_error",
                "invalid_payload",
                route=route,
                actor=actor,
            ),
            status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
        )

    try:
        offset = int(raw_offset)
        if offset < 0:
            raise ValueError("hud.status query 'offset' must be >= 0")
    except (TypeError, ValueError) as exc:
        return web.json_response(
            hud_error_payload(
                str(exc),
                "validation_error",
                "invalid_payload",
                route=route,
                actor=actor,
            ),
            status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
        )

    user_id = hud_resolve_user_id(request)
    blocked = hud_onboarding_gate_response_if_blocked(
        store=store,
        route=route,
        actor=actor,
        user_id=user_id,
    )
    if blocked is not None:
        return blocked

    push_blocked = hud_push_policy_gate_response_if_blocked(
        store=store,
        route=route,
        actor=actor,
        user_id=user_id,
    )
    if push_blocked is not None:
        return push_blocked

    try:
        items = store.list_items(status=status_value, limit=limit, offset=offset)
    except sqlite3.DatabaseError as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)
    except Exception as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)

    status_data = hud_status_data(items)
    status_data["adapter_sync_previews"] = await hud_adapter_sync_previews(hub)
    status_data = finalize_hud_data(status_data, store=store, user_id=user_id)

    return web.json_response(
        hud_success_payload(route, status="ok", actor=actor, data=status_data),
        status=200,
    )


async def handle_brief(request: web.Request) -> web.Response:
    actor = hud_actor(request)
    admin_error = await require_hud_admin(request)
    if admin_error is not None:
        return admin_error

    store: HUDStore = request.app["hud_store"]
    workers: HUDWorkers = request.app["hud_workers"]
    hub: HUDAdapterHub = request.app["hud_adapter_hub"]

    body = await request.text()
    scope_source = request.query.get("scope")
    payload: Dict[str, Any] = {}

    if body.strip():
        try:
            payload = require_json(body)
            scope_source = payload.get("scope", scope_source)
        except ValueError as exc:
            return web.json_response(
                hud_error_payload(
                    str(exc),
                    "validation_error",
                    "invalid_payload",
                    route=HUD_ROUTE_BRIEF,
                    actor=actor,
                ),
                status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
            )

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
                    route=HUD_ROUTE_BRIEF,
                    actor=actor,
                ),
                status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
            )
    else:
        scope = None

    user_id = hud_resolve_user_id(request, payload=payload)

    if explicit_scope:
        blocked = hud_onboarding_gate_response_if_blocked(
            store=store,
            route=HUD_ROUTE_BRIEF,
            actor=actor,
            user_id=user_id,
        )
        if blocked is not None:
            return blocked
        push_blocked = hud_push_policy_gate_response_if_blocked(
            store=store,
            route=HUD_ROUTE_BRIEF,
            actor=actor,
            user_id=user_id,
        )
        if push_blocked is not None:
            return push_blocked

    if not explicit_scope:
        try:
            soul_content = hud_soul_md_path().read_text(encoding="utf-8")
        except OSError as exc:
            return hud_store_error_response(route=HUD_ROUTE_BRIEF, actor=actor, exc=exc)

        classification, projection = hud_classify_project_pair(
            workers, {"intent": HUD_INTENT_BRIEF}, intent=HUD_INTENT_BRIEF
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
                HUD_ROUTE_BRIEF,
                status="ok",
                actor=actor,
                route_meta=hud_route_meta(workers, HUD_INTENT_BRIEF),
                data=brief_data,
            ),
            status=200,
        )

    try:
        items = store.list_items(status="queued", limit=100, offset=0)
        pending_approval_items = store.list_items(
            status="pending_approval", limit=100, offset=0
        )
    except sqlite3.DatabaseError as exc:
        return hud_store_error_response(route=HUD_ROUTE_BRIEF, actor=actor, exc=exc)
    except Exception as exc:
        return hud_store_error_response(route=HUD_ROUTE_BRIEF, actor=actor, exc=exc)

    all_items = items + pending_approval_items
    scoped_items = [item for item in all_items if item.get("scope") == scope]
    deterministic_items = hud_deterministic_items(scoped_items)
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

    brief_issue_data = hud_sync_issue_data(deterministic_items)
    adapter_sync_previews = await hud_adapter_sync_previews(hub)

    scoped_data = finalize_hud_data(
        {
            "scope": scope,
            "items": deterministic_items,
            "next_action": next_action,
            "onboarding_needed": onboarding_needed,
            "onboarding_needed_reason": onboarding_context,
            **policy_fields,
            **brief_issue_data,
            "adapter_sync_previews": adapter_sync_previews,
        },
        store=store,
        user_id=user_id,
    )
    return web.json_response(
        hud_success_payload(
            HUD_ROUTE_BRIEF,
            status="ok",
            actor=actor,
            route_meta=hud_route_meta(workers, HUD_INTENT_BRIEF),
            data=scoped_data,
        ),
        status=200,
    )


async def handle_health(_request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "service": "AdventedHUD"}, status=200)
