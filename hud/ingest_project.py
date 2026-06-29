"""Shared ingest and project handlers for HTTP and MCP."""

from __future__ import annotations

import logging
import sqlite3
import uuid
from typing import Any, Dict, Mapping, Optional

from aiohttp import web

from hud.adapters import HUDAdapterHub
from hud.contracts import (
    HUD_ERROR_HTTP_STATUS,
    HUD_INTENT_INGEST,
    HUD_INTENT_PROJECT,
    HUD_PROJECTION_MODE_LIVE,
    HUD_ROUTE_INGEST,
    HUD_ROUTE_PROJECT,
    hud_error_payload,
    hud_success_payload,
    normalize_projection_mode,
    parse_hud_scope,
    require_json,
    validate_hud_id,
)
from hud.gates import (
    hud_effective_projection_mode,
    hud_normalize_identifier,
    hud_onboarding_gate_response_if_blocked,
    hud_parse_explicit_bool,
    hud_projection_mode_client_fields,
    hud_push_policy_gate_response_if_blocked,
    hud_resolve_user_id,
    require_hud_admin,
)
from hud.handlers import (
    hud_classify_project_pair,
    hud_route_meta,
    hud_store_error_response,
)
from hud.onboarding import (
    hud_apply_user_onboarding_context,
    hud_extract_default_requires_approval,
    hud_store_user_onboarding_state,
)
from hud.onboarding_db import is_user_fully_onboarded
from hud.meta import (
    build_ingest_mcp_meta,
    build_rich_error_meta,
    finalize_hud_data,
    get_current_mcp_mode_meta,
)
from hud.store import HUDStore
from hud.workers import HUDWorkers

logger = logging.getLogger(__name__)

_HUD_TERMINAL_STATUSES = frozenset({"approved", "rejected", "failed", "duplicate", "synced"})
_HUD_PROJECTION_DISPATCH_STATUSES = frozenset({"queued", "approved"})


def hud_google_target_projects_externally(google_target: Any) -> bool:
    gt = str(google_target or "").strip().lower()
    return gt in ("calendar", "gcal", "google_calendar", "tasks", "gtasks")




def _ingest_explicit_requires_approval(payload: Mapping[str, Any]) -> bool:
    raw = payload.get("requires_approval")
    if raw is True:
        return True
    if isinstance(raw, str) and raw.strip().lower() in {"true", "1", "yes"}:
        return True
    if raw == 1:
        return True
    return False


def hud_build_deferred_google_projection(
    *,
    google_target: str,
    item_status: str,
    projection_mode: str,
    reason: str = "awaiting_approval",
) -> Dict[str, Any]:
    return {
        "status": "deferred",
        "reason": reason,
        "google_target": google_target,
        "item_status": item_status,
        "projection_mode": projection_mode,
        "message": "Google projection deferred until item is approved",
    }


def hud_ingest_auto_approve_push(
    store: HUDStore, user_id: str, projection_mode: str
) -> bool:
    if projection_mode != HUD_PROJECTION_MODE_LIVE or not user_id:
        return False
    try:
        return store.get_user_push_policy(user_id) is True
    except Exception:
        logger.exception("Failed to read push policy for ingest user_id=%s", user_id)
        return False


async def hud_resolve_google_projection_for_ingest(
    hub: HUDAdapterHub,
    *,
    item: Dict[str, Any],
    actor: Optional[str],
    google_target: str,
    item_status: str,
    projection_mode: str,
    auto_approve_push: bool,
) -> Optional[Dict[str, Any]]:
    if not hud_google_target_projects_externally(google_target):
        return None
    if auto_approve_push and hud_projection_can_dispatch(item_status, projection_mode):
        return await _hud_google_projection_dispatch(
            hub,
            item,
            actor,
            item_status,
            projection_mode=projection_mode,
        )
    reason = "awaiting_approval"
    if item_status == "queued" and not auto_approve_push:
        reason = "awaiting_approval"
    return hud_build_deferred_google_projection(
        google_target=google_target,
        item_status=item_status,
        projection_mode=projection_mode,
        reason=reason,
    )
def hud_is_terminal_status(status: Any) -> bool:
    return str(status or "").strip().lower() in _HUD_TERMINAL_STATUSES


def hud_projection_can_dispatch(status: Any, projection_mode: str) -> bool:
    normalized = str(status or "").strip().lower()
    return (
        projection_mode == HUD_PROJECTION_MODE_LIVE
        and normalized in _HUD_PROJECTION_DISPATCH_STATUSES
    )


def hud_projection_noop_result(
    intent: str, *, status: str, projection_mode: str, reason: str
) -> Dict[str, Any]:
    return {
        "status": "noop",
        "intent": intent,
        "adapter": None,
        "action": "noop",
        "result": {
            "status": "ok",
            "message": reason,
            "item_status": status,
            "projection_mode": projection_mode,
        },
    }


def hud_storage_payload(payload: Mapping[str, Any]) -> Dict[str, Any]:
    sanitized = dict(payload)
    sanitized.pop("oauth_credentials", None)
    sanitized.pop("token_credentials", None)
    sanitized.pop("onboarding_needed_reason", None)
    return sanitized


def hud_extract_source_id(payload: Mapping[str, Any]) -> Optional[str]:
    for key in (
        "source_id",
        "source_ref",
        "goal_id",
        "role_id",
        "event_id",
        "task_id",
        "external_id",
        "google_id",
    ):
        value = payload.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def hud_payload_with_hud_metadata(
    payload: Mapping[str, Any],
    *,
    classification: Mapping[str, Any],
    projection: Mapping[str, Any],
) -> Dict[str, Any]:
    enriched = dict(payload)
    enriched["classification"] = dict(classification)
    enriched["projection"] = dict(projection)
    return enriched


def hud_projection_payload_for_item(
    item: Dict[str, Any],
    actor: Optional[str],
    status: str,
    *,
    projection_mode: Optional[str] = None,
) -> Dict[str, Any]:
    base_payload = item.get("payload_json")
    if not isinstance(base_payload, dict):
        base_payload = {}
    projection_payload = dict(base_payload)
    resolved_mode = normalize_projection_mode(projection_mode) or "dry_run"
    projection_payload["projection_mode"] = resolved_mode
    if "intent" not in projection_payload:
        projection_payload["intent"] = item.get("intent") or HUD_INTENT_INGEST
    if not projection_payload.get("scope"):
        projection_payload["scope"] = item.get("scope")
    if not projection_payload.get("status"):
        projection_payload["status"] = status
    if not projection_payload.get("item_id"):
        projection_payload["item_id"] = item.get("internal_id")
    if actor and not projection_payload.get("actor"):
        projection_payload["actor"] = actor
    for key in (
        "classification",
        "projection",
        "goal_ref",
        "role_ref",
        "semantic_type",
        "google_target",
        "adapter_target",
        "adapter_method",
        "requires_approval",
        "source_id",
        "last_synced_at",
    ):
        if key in item and key not in projection_payload:
            projection_payload[key] = item.get(key)
    return {
        "intent": item.get("intent")
        or (item.get("payload_json") or {}).get("intent", HUD_INTENT_INGEST),
        "scope": item.get("scope"),
        "actor": actor,
        "status": status,
        "projection_mode": resolved_mode,
        "payload": projection_payload,
        "item_id": item.get("internal_id"),
    }


async def hud_dispatch_projection(
    hub: HUDAdapterHub, intent: Any, payload: Dict[str, Any]
) -> Dict[str, Any]:
    try:
        return await hub.dispatch(intent, payload)
    except Exception as exc:
        logger.warning("HUD adapter dispatch failed intent=%s: %s", intent, exc, exc_info=True)
        return {
            "status": "error",
            "intent": str(intent),
            "adapter": None,
            "action": None,
            "error": {"code": "dispatch_exception", "message": str(exc)},
            "result": {"status": "error", "message": str(exc)},
        }


async def _hud_google_projection_dispatch(
    hub: HUDAdapterHub,
    item: Dict[str, Any],
    actor: Optional[str],
    status: str,
    *,
    projection_mode: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """Secondary dispatch to Google adapter when google_target is set.

    Called after the primary Obsidian dispatch. The Google adapter's own
    status gate (approved check) determines whether it actually fires.
    """
    google_target = (item.get("google_target") or "").lower()
    if not google_target:
        return None
    if google_target in ("obsidian", "base", "role", "goal"):
        return None
    if not hud_google_target_projects_externally(google_target):
        logger.warning(
            "Google projection: unsupported google_target=%s item=%s",
            google_target,
            item.get("internal_id", "?"),
        )
        return {
            "status": "error",
            "code": "unknown_google_target",
            "message": "Unsupported google_target: %s" % google_target,
        }

    payload_dict = hud_projection_payload_for_item(
        item, actor, status, projection_mode=projection_mode,
    )

    if google_target in ("tasks", "gtasks"):
        logger.info("Google projection: gtasks.upsert status=%s item=%s", status, item.get("internal_id", "?"))
        return await hud_dispatch_projection(hub, "gtasks.upsert", payload_dict)
    if google_target in ("calendar", "gcal", "google_calendar"):
        logger.info("Google projection: gcal.upsert status=%s item=%s", status, item.get("internal_id", "?"))
        return await hud_dispatch_projection(hub, "gcal.upsert", payload_dict)

    return {
        "status": "error",
        "code": "unknown_google_target",
        "message": "Unsupported google_target: %s" % google_target,
    }


def hud_adapter_projection_failed(adapter_projection: Dict[str, Any]) -> bool:
    if str(adapter_projection.get("status", "")).strip().lower() in {"error", "blocked"}:
        return True
    result = adapter_projection.get("result")
    return isinstance(result, dict) and str(result.get("status", "")).strip().lower() in {
        "error",
        "blocked",
    }


def hud_adapter_projection_error_message(
    adapter_projection: Dict[str, Any], *, fallback_intent: Optional[str] = None
) -> str:
    error = adapter_projection.get("error")
    if isinstance(error, dict):
        message = error.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    result = adapter_projection.get("result")
    if isinstance(result, dict):
        message = result.get("message")
        if isinstance(message, str) and message.strip():
            return message.strip()
    status = adapter_projection.get("status", "error")
    intent = fallback_intent or adapter_projection.get("intent") or "unknown"
    return f"Adapter projection failed for intent '{intent}' with status '{status}'"


def hud_not_found_error_response(
    *, route: str, actor: Optional[str], item_id: str
) -> web.Response:
    return web.json_response(
        hud_error_payload(
            f"HUD item '{item_id}' not found",
            "item_not_found",
            "item_not_found",
            route=route,
            actor=actor,
        ),
        status=404,
    )


def hud_next_status_for_intent(
    workers: HUDWorkers, intent: str, *, current_status: Optional[str] = None
) -> Optional[str]:
    classification = workers.classify({"intent": intent, "status": current_status or ""})
    transition = classification.get("status_transition", {})
    if isinstance(transition, Mapping):
        target = transition.get("to")
        if isinstance(target, str) and target.strip():
            return target
    return None


async def execute_hud_ingest(
    request: web.Request,
    *,
    store: HUDStore,
    workers: HUDWorkers,
    hub: HUDAdapterHub,
    payload: Mapping[str, Any],
    route: str,
    actor: Optional[str],
    route_meta: Optional[Dict[str, Any]] = None,
) -> web.Response:
    user_id = hud_resolve_user_id(request, payload=payload)
    try:
        scope = parse_hud_scope(payload.get("scope"))
    except ValueError as exc:
        err = hud_error_payload(
            str(exc),
            "validation_error",
            "invalid_payload",
            route=route,
            actor=actor,
        )
        err.setdefault("data", {})["mcp_meta"] = build_rich_error_meta(
            current_mode="onboarding" if not is_user_fully_onboarded(store, user_id) else "operational",
            tool="hud.ingest",
            violation=f"invalid scope: {exc}",
            guidance="Provide a valid scope (or omit for default). Re-issue the call after fixing the payload.",
        )
        return web.json_response(err, status=HUD_ERROR_HTTP_STATUS["invalid_payload"])

    try:
        projection_bundle = hud_effective_projection_mode(
            request, store=store, user_id=user_id, payload=payload
        )
    except ValueError as exc:
        err = hud_error_payload(
            str(exc),
            "validation_error",
            "invalid_payload",
            route=route,
            actor=actor,
        )
        err.setdefault("data", {})["mcp_meta"] = build_rich_error_meta(
            current_mode="onboarding" if not is_user_fully_onboarded(store, user_id) else "operational",
            tool="hud.ingest",
            violation=f"invalid projection_mode: {exc}",
            guidance="Use 'dry_run', 'live', or omit (defaults to dry_run or stored preference).",
        )
        return web.json_response(err, status=HUD_ERROR_HTTP_STATUS["invalid_payload"])
    except sqlite3.DatabaseError as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)
    except Exception as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)

    projection_mode = projection_bundle["effective_projection_mode"]
    internal_id = payload.get("item_id", payload.get("internal_id"))
    if internal_id is not None:
        try:
            internal_id = validate_hud_id(internal_id)
        except ValueError as exc:
            err = hud_error_payload(
                str(exc),
                "validation_error",
                "invalid_payload",
                route=route,
                actor=actor,
            )
            err.setdefault("data", {})["mcp_meta"] = build_rich_error_meta(
                current_mode="onboarding" if not is_user_fully_onboarded(store, user_id) else "operational",
                tool="hud.ingest",
                violation=f"invalid item_id / internal_id: {exc}",
                guidance="item_id must be a valid identifier (hex) or omitted (we will generate one).",
            )
            return web.json_response(err, status=HUD_ERROR_HTTP_STATUS["invalid_payload"])
    else:
        internal_id = uuid.uuid4().hex

    ingest_payload = hud_apply_user_onboarding_context(
        dict(payload), store=store, user_id=user_id
    )
    auto_approve_push = hud_ingest_auto_approve_push(store, user_id, projection_mode)
    queue_explicit = _ingest_explicit_requires_approval(ingest_payload)
    if auto_approve_push and not queue_explicit:
        ingest_payload["requires_approval"] = False
    onboarding_needed = bool(ingest_payload.get("onboarding_needed"))
    onboarding_needed_reason = ingest_payload.get("onboarding_needed_reason")
    idempotency_key = ingest_payload.get("idempotency", ingest_payload.get("idempotency_key"))
    if idempotency_key is not None and not isinstance(idempotency_key, str):
        idempotency_key = str(idempotency_key)

    ingest_intent = ingest_payload.get("intent", HUD_INTENT_INGEST)
    classification, projection = hud_classify_project_pair(
        workers,
        ingest_payload,
        intent=ingest_intent,
        status="queued",
        projection_mode=projection_mode,
    )
    initial_status = "pending_approval" if projection.get("requires_approval") else "queued"
    if auto_approve_push and not queue_explicit:
        initial_status = "approved"
    if initial_status == "pending_approval":
        classification, projection = hud_classify_project_pair(
            workers,
            ingest_payload,
            intent=ingest_intent,
            status=initial_status,
            projection_mode=projection_mode,
        )
    elif initial_status == "approved":
        classification, projection = hud_classify_project_pair(
            workers,
            ingest_payload,
            intent=ingest_intent,
            status=initial_status,
            projection_mode=projection_mode,
        )

    try:
        hud_store_user_onboarding_state(
            store,
            user_id,
            payload=ingest_payload,
            classification=classification,
        )
    except Exception:
        logger.exception("Failed to persist onboarding context during ingest user_id=%s", user_id)

    storage_payload = hud_payload_with_hud_metadata(
        hud_storage_payload(ingest_payload),
        classification=classification,
        projection=projection,
    )

    try:
        item = store.upsert_item(
            {
                "internal_id": internal_id,
                "google_id": ingest_payload.get("google_id"),
                "external_id": ingest_payload.get("external_id"),
                "actor": actor,
                "intent": ingest_intent,
                "scope": scope,
                "payload_json": storage_payload,
                "status": initial_status,
                "priority_class": classification.get("priority_class"),
                "google_target": classification.get("google_target"),
                "semantic_type": classification.get("semantic_type"),
                "role_ref": classification.get("role_ref"),
                "goal_ref": classification.get("goal_ref"),
                "idempotency_key": idempotency_key,
                "source_id": hud_extract_source_id(ingest_payload),
                "last_synced_at": ingest_payload.get("last_synced_at"),
            }
        )
    except sqlite3.DatabaseError as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)
    except Exception as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)

    if hud_projection_can_dispatch(item.get("status"), projection_mode):
        adapter_projection = await hud_dispatch_projection(
            hub,
            item.get("intent") or ingest_intent,
            hud_projection_payload_for_item(
                item,
                actor,
                initial_status,
                projection_mode=projection_mode,
            ),
        )
    else:
        reason = "projection deferred until approval"
        if projection_mode != HUD_PROJECTION_MODE_LIVE:
            reason = "projection_mode is preview-only"
        adapter_projection = hud_projection_noop_result(
            item.get("intent") or ingest_intent,
            status=item.get("status") or initial_status,
            projection_mode=projection_mode,
            reason=reason,
        )

    # Google projection: deferred on ingest unless push-without-approval (Pattern B)
    resolved_google_target = (
        classification.get("google_target")
        or item.get("google_target")
        or ""
    )
    google_projection = await hud_resolve_google_projection_for_ingest(
        hub,
        item=item,
        actor=actor,
        google_target=str(resolved_google_target),
        item_status=item.get("status") or initial_status,
        projection_mode=projection_mode,
        auto_approve_push=auto_approve_push,
    )


    if google_projection is not None:
        adapter_projection = dict(adapter_projection)
        adapter_projection["external_dispatch"] = google_projection

    ingest_data = finalize_hud_data(
        {
            "item": item,
            "adapter_projection": adapter_projection,
            "onboarding_needed": onboarding_needed,
            "onboarding_needed_reason": onboarding_needed_reason,
            "classification": classification,
            "projection": projection,
            **hud_projection_mode_client_fields(projection_bundle),
        },
        store=store,
        user_id=user_id,
    )
    ingest_data["mcp_meta"] = build_ingest_mcp_meta(
        store,
        user_id,
        google_target=str(classification.get("google_target") or item.get("google_target") or ""),
        semantic_type=str(classification.get("semantic_type") or item.get("semantic_type") or ""),
    )
    return web.json_response(
        hud_success_payload(
            route,
            status="ok",
            actor=actor,
            route_meta=route_meta,
            data=ingest_data,
        ),
        status=200,
    )


async def execute_hud_project_approve_reject(
    request: web.Request,
    *,
    store: HUDStore,
    workers: HUDWorkers,
    hub: HUDAdapterHub,
    params: Mapping[str, Any],
    action: str,
    route: str,
    actor: Optional[str],
    route_meta: Optional[Dict[str, Any]] = None,
) -> web.Response:
    user_id = hud_resolve_user_id(request, payload=params)
    try:
        projection_bundle = hud_effective_projection_mode(
            request, store=store, user_id=user_id, payload=params
        )
    except ValueError as exc:
        err = hud_error_payload(
            str(exc),
            "validation_error",
            "invalid_payload",
            route=route,
            actor=actor,
        )
        err.setdefault("data", {})["mcp_meta"] = build_rich_error_meta(
            current_mode="onboarding" if not is_user_fully_onboarded(store, user_id) else "operational",
            tool="hud.project",
            violation=f"invalid projection_mode: {exc}",
            guidance="Use 'dry_run', 'live', or omit.",
        )
        return web.json_response(err, status=HUD_ERROR_HTTP_STATUS["invalid_payload"])
    except sqlite3.DatabaseError as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)
    except Exception as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)

    projection_mode = projection_bundle["effective_projection_mode"]
    item_id = params.get("item_id") or params.get("internal_id")
    if item_id is None or (isinstance(item_id, str) and not item_id.strip()):
        err = hud_error_payload(
            "item_id is required for approve/reject action via hud.project",
            "validation_error",
            "invalid_payload",
            route=route,
            actor=actor,
        )
        err.setdefault("data", {})["mcp_meta"] = build_rich_error_meta(
            current_mode="onboarding" if not is_user_fully_onboarded(store, user_id) else "operational",
            tool="hud.project",
            violation="missing item_id for approve/reject",
            guidance="Provide the item_id (or internal_id) of a previously ingested item. Use hud.brief to discover pending items if needed.",
        )
        return web.json_response(err, status=HUD_ERROR_HTTP_STATUS["invalid_payload"])
    try:
        item_id = validate_hud_id(str(item_id))
    except ValueError as exc:
        err = hud_error_payload(
            str(exc),
            "validation_error",
            "invalid_item_id",
            route=route,
            actor=actor,
        )
        err.setdefault("data", {})["mcp_meta"] = build_rich_error_meta(
            current_mode="onboarding" if not is_user_fully_onboarded(store, user_id) else "operational",
            tool="hud.project",
            violation=f"invalid item_id: {exc}",
            guidance="item_id must be a valid hex id returned by a prior hud.ingest or hud.brief.",
        )
        return web.json_response(err, status=HUD_ERROR_HTTP_STATUS["invalid_item_id"])

    try:
        item = store.get_item(item_id)
    except sqlite3.DatabaseError as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)
    except Exception as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)

    if item is None:
        return hud_not_found_error_response(route=route, actor=actor, item_id=item_id)

    if hud_is_terminal_status(item.get("status")):
        terminal_data = finalize_hud_data(
            {
                "item": item,
                "adapter_projection": hud_projection_noop_result(
                    item.get("intent", action),
                    status=item.get("status") or "",
                    projection_mode=projection_mode,
                    reason="Item is already in terminal state",
                ),
                **hud_projection_mode_client_fields(projection_bundle),
            },
            store=store,
            user_id=user_id,
        )
        return web.json_response(
            hud_success_payload(
                route,
                status="ok",
                actor=actor,
                route_meta=route_meta,
                data=terminal_data,
            ),
            status=200,
        )

    try:
        target_status = hud_next_status_for_intent(
            workers, action, current_status=item.get("status")
        )
        if target_status is None:
            target_status = "approved" if action == "approve" else "rejected"
        updated = store.transition_status(
            item_id,
            target_status,
            actor=actor,
            reviewed_by=actor,
        )
    except ValueError as exc:
        err = hud_error_payload(
            str(exc),
            "validation_error",
            "invalid_status_transition",
            route=route,
            actor=actor,
        )
        err.setdefault("data", {})["mcp_meta"] = build_rich_error_meta(
            current_mode="onboarding" if not is_user_fully_onboarded(store, user_id) else "operational",
            tool="hud.project",
            violation=f"invalid status transition: {exc}",
            guidance="Only approved/rejected transitions from queued/pending_approval are allowed via the action. Check current item status via hud.brief first.",
        )
        return web.json_response(err, status=HUD_ERROR_HTTP_STATUS["validation_error"])
    except sqlite3.DatabaseError as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)
    except Exception as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)

    if not updated:
        return hud_not_found_error_response(route=route, actor=actor, item_id=item_id)

    try:
        item = store.get_item(item_id)
    except sqlite3.DatabaseError as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)
    except Exception as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)

    if item is None:
        return hud_not_found_error_response(route=route, actor=actor, item_id=item_id)

    if action == "approve" and hud_projection_can_dispatch(item.get("status"), projection_mode):
        adapter_projection = await hud_dispatch_projection(
            hub,
            item.get("intent", "ingest"),
            hud_projection_payload_for_item(
                item,
                actor,
                target_status,
                projection_mode=projection_mode,
            ),
        )
        if hud_adapter_projection_failed(adapter_projection):
            last_error = hud_adapter_projection_error_message(
                adapter_projection, fallback_intent=item.get("intent", "ingest")
            )
            try:
                store.update_status(
                    item_id, "failed", actor=actor, reviewed_by=actor, last_error=last_error
                )
            except Exception:
                pass
            failure_payload = hud_error_payload(
                last_error,
                "adapter_error",
                "adapter_projection_failed",
                route=route,
                actor=actor,
            )
            failure_payload["adapter_projection"] = adapter_projection
            failure_payload["data"] = finalize_hud_data(
                {"item": item},
                store=store,
                user_id=user_id,
            )
            return web.json_response(failure_payload, status=500)
    else:
        reason = "projection dispatch skipped for reject or non-live mode"
        if projection_mode != HUD_PROJECTION_MODE_LIVE:
            reason = "projection_mode is preview-only"
        adapter_projection = hud_projection_noop_result(
            item.get("intent", action),
            status=target_status,
            projection_mode=projection_mode,
            reason=reason,
        )

    # Secondary Google projection dispatch
    google_projection = await _hud_google_projection_dispatch(
        hub, item, actor, target_status,
        projection_mode=projection_mode,
    )


    if google_projection is not None:
        adapter_projection = dict(adapter_projection)
        adapter_projection["external_dispatch"] = google_projection

    approve_data = finalize_hud_data(
        {
            "item": item,
            "adapter_projection": adapter_projection,
            **hud_projection_mode_client_fields(projection_bundle),
        },
        store=store,
        user_id=user_id,
    )
    return web.json_response(
        hud_success_payload(
            route,
            status="ok",
            actor=actor,
            route_meta=route_meta,
            data=approve_data,
        ),
        status=200,
    )


async def execute_hud_project_default(
    request: web.Request,
    *,
    store: HUDStore,
    workers: HUDWorkers,
    params: Mapping[str, Any],
    route: str,
    actor: Optional[str],
    route_meta: Optional[Dict[str, Any]] = None,
) -> web.Response:
    user_id = hud_resolve_user_id(request, payload=params)
    try:
        projection_bundle = hud_effective_projection_mode(
            request, store=store, user_id=user_id, payload=params
        )
    except ValueError as exc:
        err = hud_error_payload(
            str(exc),
            "validation_error",
            "invalid_payload",
            route=route,
            actor=actor,
        )
        err.setdefault("data", {})["mcp_meta"] = build_rich_error_meta(
            current_mode="onboarding" if not is_user_fully_onboarded(store, user_id) else "operational",
            tool="hud.project",
            violation=f"invalid projection_mode: {exc}",
            guidance="Use 'dry_run', 'live', or omit.",
        )
        return web.json_response(err, status=HUD_ERROR_HTTP_STATUS["invalid_payload"])
    except sqlite3.DatabaseError as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)
    except Exception as exc:
        return hud_store_error_response(route=route, actor=actor, exc=exc)

    projection_mode = projection_bundle["effective_projection_mode"]
    enriched = hud_apply_user_onboarding_context(dict(params), store=store, user_id=user_id)
    onboarding_needed = bool(enriched.get("onboarding_needed"))
    onboarding_needed_reason = enriched.get("onboarding_needed_reason")
    classification, projection = hud_classify_project_pair(
        workers,
        enriched,
        intent=HUD_INTENT_PROJECT,
        status=enriched.get("status") or "queued",
        projection_mode=projection_mode,
    )
    try:
        hud_store_user_onboarding_state(
            store,
            user_id,
            payload=enriched,
            classification=classification,
        )
    except Exception:
        logger.exception("Failed to persist onboarding context during project user_id=%s", user_id)

    item_ref = enriched.get("item_id", enriched.get("internal_id"))
    if item_ref is not None and not isinstance(item_ref, str):
        item_ref = str(item_ref)
    if item_ref:
        try:
            existing_item = store.get_item(item_ref)
        except sqlite3.DatabaseError as exc:
            return hud_store_error_response(route=route, actor=actor, exc=exc)
        except Exception as exc:
            return hud_store_error_response(route=route, actor=actor, exc=exc)
        if existing_item is not None:
            storage_payload = hud_payload_with_hud_metadata(
                hud_storage_payload(existing_item.get("payload_json", {})),
                classification=classification,
                projection=projection,
            )
            try:
                store.upsert_item(
                    {
                        "internal_id": item_ref,
                        "google_id": existing_item.get("google_id"),
                        "external_id": existing_item.get("external_id"),
                        "actor": actor or existing_item.get("actor"),
                        "intent": existing_item.get("intent") or HUD_INTENT_PROJECT,
                        "scope": existing_item.get("scope"),
                        "payload_json": storage_payload,
                        "status": existing_item.get("status") or "pending",
                        "priority_class": classification.get("priority_class"),
                        "google_target": classification.get("google_target"),
                        "semantic_type": classification.get("semantic_type"),
                        "role_ref": classification.get("role_ref"),
                        "goal_ref": classification.get("goal_ref"),
                        "idempotency_key": existing_item.get("idempotency_key"),
                        "source_id": existing_item.get("source_id")
                        or hud_extract_source_id(enriched),
                        "last_synced_at": existing_item.get("last_synced_at")
                        or enriched.get("last_synced_at"),
                        "next_run_at": existing_item.get("next_run_at"),
                        "approved_by": existing_item.get("approved_by"),
                        "reviewed_by": existing_item.get("reviewed_by"),
                        "retry_count": existing_item.get("retry_count", 0),
                    }
                )
            except sqlite3.DatabaseError as exc:
                return hud_store_error_response(route=route, actor=actor, exc=exc)
            except Exception as exc:
                return hud_store_error_response(route=route, actor=actor, exc=exc)

    project_data = finalize_hud_data(
        {
            "intent": HUD_INTENT_PROJECT,
            "payload": enriched,
            "onboarding_needed": onboarding_needed,
            "onboarding_needed_reason": onboarding_needed_reason,
            "classification": classification,
            "projection": projection,
            **hud_projection_mode_client_fields(projection_bundle),
        },
        store=store,
        user_id=user_id,
    )
    return web.json_response(
        hud_success_payload(
            route,
            status="ok",
            actor=actor,
            route_meta=route_meta,
            data=project_data,
        ),
        status=200,
    )


async def handle_hud_ingest(request: web.Request) -> web.Response:
    from hud.gates import hud_actor

    actor = hud_actor(request)
    admin_error = await require_hud_admin(request)
    if admin_error is not None:
        return admin_error

    store: HUDStore = request.app["hud_store"]
    workers: HUDWorkers = request.app["hud_workers"]
    hub: HUDAdapterHub = request.app["hud_adapter_hub"]

    try:
        payload = require_json(await request.text())
    except ValueError as exc:
        return web.json_response(
            hud_error_payload(
                str(exc),
                "validation_error",
                "invalid_payload",
                route=HUD_ROUTE_INGEST,
                actor=actor,
            ),
            status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
        )

    user_id = hud_resolve_user_id(request, payload=payload)
    blocked = hud_onboarding_gate_response_if_blocked(
        store=store,
        route=HUD_ROUTE_INGEST,
        actor=actor,
        user_id=user_id,
    )
    if blocked is not None:
        return blocked
    push_blocked = hud_push_policy_gate_response_if_blocked(
        store=store,
        route=HUD_ROUTE_INGEST,
        actor=actor,
        user_id=user_id,
    )
    if push_blocked is not None:
        return push_blocked

    return await execute_hud_ingest(
        request,
        store=store,
        workers=workers,
        hub=hub,
        payload=payload,
        route=HUD_ROUTE_INGEST,
        actor=actor,
        route_meta=hud_route_meta(workers, HUD_INTENT_INGEST),
    )


async def handle_hud_project(request: web.Request) -> web.Response:
    from hud.gates import hud_actor

    actor = hud_actor(request)
    admin_error = await require_hud_admin(request)
    if admin_error is not None:
        return admin_error

    store: HUDStore = request.app["hud_store"]
    workers: HUDWorkers = request.app["hud_workers"]
    hub: HUDAdapterHub = request.app["hud_adapter_hub"]

    user_id = hud_resolve_user_id(request)
    blocked = hud_onboarding_gate_response_if_blocked(
        store=store,
        route=HUD_ROUTE_PROJECT,
        actor=actor,
        user_id=user_id,
    )
    if blocked is not None:
        return blocked

    body = await request.text()
    if body.strip():
        try:
            payload = require_json(body)
        except ValueError as exc:
            return web.json_response(
                hud_error_payload(
                    str(exc),
                    "validation_error",
                    "invalid_payload",
                    route=HUD_ROUTE_PROJECT,
                    actor=actor,
                ),
                status=HUD_ERROR_HTTP_STATUS["invalid_payload"],
            )
    else:
        payload = {}

    user_id = hud_resolve_user_id(request, payload=payload)
    push_blocked = hud_push_policy_gate_response_if_blocked(
        store=store,
        route=HUD_ROUTE_PROJECT,
        actor=actor,
        user_id=user_id,
    )
    if push_blocked is not None:
        return push_blocked

    action = str(payload.get("action") or payload.get("fate") or "project").strip().lower()
    if action not in ("project", "approve", "reject"):
        action = "project"

    route_meta = hud_route_meta(workers, action if action != "project" else HUD_INTENT_PROJECT)
    if action in ("approve", "reject"):
        return await execute_hud_project_approve_reject(
            request,
            store=store,
            workers=workers,
            hub=hub,
            params=payload,
            action=action,
            route=HUD_ROUTE_PROJECT,
            actor=actor,
            route_meta=route_meta,
        )

    return await execute_hud_project_default(
        request,
        store=store,
        workers=workers,
        params=payload,
        route=HUD_ROUTE_PROJECT,
        actor=actor,
        route_meta=route_meta,
    )
