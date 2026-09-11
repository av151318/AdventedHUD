"""HUD request gates: admin auth, user id, onboarding, push policy."""

from __future__ import annotations

import base64
import json
import logging
import os
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from aiohttp import web

from hud.contracts import (
    HUD_ERROR_HTTP_STATUS,
    HUD_PROJECTION_MODE_DRY_RUN,
    HUD_PROJECTION_MODE_LIVE,
    HUD_ROUTE_MCP,
    hud_error_payload,
    normalize_projection_mode,
    parse_projection_mode,
)
from hud.meta import build_rich_error_meta, finalize_hud_data, get_current_mcp_mode_meta
from hud.store import HUDStore

logger = logging.getLogger(__name__)

HUD_PACKAGE_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOUL_PATH = HUD_PACKAGE_ROOT.parent / "data" / "obsidian" / "AdventedHUD" / "soul.md"


def hud_actor(request: web.Request) -> Optional[str]:
    actor = request.headers.get("X-HUD-Actor") or request.headers.get("x-hud-actor")
    return actor.strip() if actor and actor.strip() else None


def hud_normalize_identifier(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def hud_resolve_user_id(
    request: web.Request,
    payload: Optional[Mapping[str, Any]] = None,
) -> str:
    if os.environ.get("HUD_RESOLVE_JWT_SUBJECT", "").strip().lower() in {"1", "true", "yes"}:
        auth = request.headers.get("Authorization") or request.headers.get("authorization")
        if isinstance(auth, str) and auth.lower().startswith("bearer "):
            token = auth.split(" ", 1)[1].strip()
            sub = hud_jwt_subject_unverified(token)
            if sub:
                return sub
    candidates = (
        request.headers.get("X-HUD-User-Id"),
        request.headers.get("x-hud-user-id"),
        request.query.get("user_id"),
    )
    if payload is not None:
        candidates += (
            payload.get("user_id"),
            payload.get("userId"),
            payload.get("actor"),
        )
    actor = hud_actor(request)
    if actor is not None:
        candidates += (actor,)
    for candidate in candidates:
        text = hud_normalize_identifier(candidate)
        if text:
            return text
    return "localuser"


def hud_jwt_subject_unverified(token: str) -> Optional[str]:
    parts = token.split(".")
    if len(parts) != 3:
        return None
    body = parts[1]
    pad = "=" * ((4 - len(body) % 4) % 4)
    try:
        decoded = base64.urlsafe_b64decode(body + pad)
        payload = json.loads(decoded.decode("utf-8"))
    except (ValueError, TypeError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    sub = payload.get("sub")
    if isinstance(sub, str) and sub.strip():
        return sub.strip()
    return None


def hud_admin_key() -> Optional[str]:
    admin_key = os.environ.get("HUD_ADMIN_API_KEY", "").strip()
    return admin_key or None


async def require_hud_admin(request: web.Request) -> Optional[web.Response]:
    actor = hud_actor(request)
    required_key = hud_admin_key()
    if not required_key:
        return web.json_response(
            hud_error_payload(
                "HUD admin key is not configured",
                "authentication_error",
                "missing_hud_admin_key",
                service="HUD",
                route=str(request.path),
                actor=actor,
            ),
            status=401,
        )
    provided_key = request.headers.get("X-HUD-Admin-Key") or request.headers.get(
        "x-hud-admin-key"
    )
    if provided_key == required_key:
        return None
    return web.json_response(
        hud_error_payload(
            "Invalid or missing HUD admin key",
            "authentication_error",
            "invalid_hud_admin_key",
            route=str(request.path),
            actor=actor,
        ),
        status=401,
    )


def _hud_admin_principal() -> Dict[str, Any]:
    return {"type": "admin"}


def _hud_agent_principal(row: Mapping[str, Any]) -> Dict[str, Any]:
    grants = row.get("grants") if isinstance(row.get("grants"), Mapping) else {}
    principal: Dict[str, Any] = {
        "type": "agent",
        "agent_id": row.get("agent_id"),
        "allowed_role_refs": list(grants.get("allowed_role_refs") or []),
        "allowed_tools": list(grants.get("allowed_tools") or []),
        "allowed_google_targets": list(grants.get("allowed_google_targets") or []),
        "grants": dict(grants),
    }
    if grants.get("calendar_id") is not None:
        principal["calendar_id"] = grants.get("calendar_id")
    if grants.get("tasklist_id") is not None:
        principal["tasklist_id"] = grants.get("tasklist_id")
    return principal


async def require_hud_principal(request: web.Request) -> Optional[web.Response]:
    """Resolve admin or agent principal. Admin short-circuit is unchanged."""
    required_key = hud_admin_key()
    provided_admin = request.headers.get("X-HUD-Admin-Key") or request.headers.get(
        "x-hud-admin-key"
    )
    if required_key and provided_admin == required_key:
        request["hud_principal"] = _hud_admin_principal()
        return None

    provided_agent = request.headers.get("X-HUD-Agent-Key") or request.headers.get(
        "x-hud-agent-key"
    )
    if provided_agent:
        store = request.app.get("hud_store") if request.app is not None else None
        lookup = getattr(store, "lookup_agent_by_key", None) if store is not None else None
        row = lookup(provided_agent) if callable(lookup) else None
        if row is not None:
            request["hud_principal"] = _hud_agent_principal(row)
            return None

    return await require_hud_admin(request)


def hud_principal(request: web.Request) -> Dict[str, Any]:
    principal = request.get("hud_principal")
    if isinstance(principal, Mapping):
        return dict(principal)
    return {}


def hud_is_admin_principal(request: web.Request) -> bool:
    return hud_principal(request).get("type") == "admin"


def hud_agent_forbidden_response(
    request: web.Request,
    *,
    message: str = "Agent is not permitted to perform this HUD operation",
    route: Optional[str] = None,
) -> web.Response:
    actor = hud_actor(request)
    return web.json_response(
        hud_error_payload(
            message,
            "authorization_error",
            "hud_agent_forbidden",
            route=route or str(request.path),
            actor=actor,
        ),
        status=403,
    )


_HUD_ONBOARDING_TOOLS = frozenset(
    {
        "hud.onboarding",
        "hud.onboarding.read",
        "hud.onboarding.write_soul",
        "hud.onboarding.set_atomic",
        "hud.onboarding.set_push",
    }
)

_HUD_ROUTE_TOOLS = {
    "/hud/brief": "hud.brief",
    "/hud/ingest": "hud.ingest",
    "/hud/project": "hud.project",
    "/hud/sync_status": "hud.sync_status",
    "/hud/status": "hud.sync_status",
    "/hud/agents/keys": "hud.agents.keys",
    "/hud/onboarding/soul": "hud.onboarding",
    "/hud/onboarding/read": "hud.onboarding.read",
    "/hud/onboarding/write_soul": "hud.onboarding.write_soul",
    "/hud/onboarding/set_atomic": "hud.onboarding.set_atomic",
    "/hud/onboarding/set_push": "hud.onboarding.set_push",
}

_GOOGLE_TARGET_ALIASES = {
    "calendar": "calendar",
    "gcal": "calendar",
    "google_calendar": "calendar",
    "tasks": "tasks",
    "gtasks": "tasks",
    "google_tasks": "tasks",
    "obsidian": "obsidian",
    "base": "obsidian",
    "role": "obsidian",
    "goal": "obsidian",
}


def hud_normalize_google_target(value: Any) -> str:
    text = str(value or "").strip().lower().replace("-", "_")
    if not text:
        return ""
    return _GOOGLE_TARGET_ALIASES.get(text, text)


def hud_tool_for_request(request: web.Request, *, mcp_method: Optional[str] = None) -> str:
    if mcp_method:
        return str(mcp_method).strip()
    path = str(request.path or "")
    return _HUD_ROUTE_TOOLS.get(path, path)


def hud_enforce_agent_tool(
    request: web.Request,
    *,
    tool: str,
    route: Optional[str] = None,
) -> Optional[web.Response]:
    """403 agent principals for disallowed tools. Admin skips. Onboarding/soul always forbidden."""
    principal = hud_principal(request)
    if principal.get("type") != "agent":
        return None
    normalized = str(tool or "").strip()
    if normalized.startswith("hud.onboarding") or normalized in _HUD_ONBOARDING_TOOLS:
        return hud_agent_forbidden_response(
            request,
            message="Agent is not permitted to use onboarding or soul rewrite",
            route=route,
        )
    allowed = [
        str(item).strip()
        for item in (principal.get("allowed_tools") or [])
        if str(item).strip()
    ]
    if normalized not in allowed:
        return hud_agent_forbidden_response(
            request,
            message=f"Agent is not permitted to use tool '{normalized}'",
            route=route,
        )
    return None


def hud_enforce_agent_role_ref(
    request: web.Request,
    payload: Optional[Mapping[str, Any]] = None,
    *,
    route: Optional[str] = None,
) -> Optional[web.Response]:
    principal = hud_principal(request)
    if principal.get("type") != "agent":
        return None
    data = payload or {}
    role = None
    for key in ("role_ref", "role", "roleId", "role_id"):
        raw = data.get(key)
        if raw is not None and str(raw).strip():
            role = str(raw).strip()
            break
    if not role:
        return None
    allowed = [
        str(item).strip()
        for item in (principal.get("allowed_role_refs") or [])
        if str(item).strip()
    ]
    if role not in allowed:
        return hud_agent_forbidden_response(
            request,
            message=f"Agent is not permitted to use role_ref '{role}'",
            route=route,
        )
    return None


def hud_enforce_agent_google_grants(
    request: web.Request,
    payload: Optional[Mapping[str, Any]] = None,
    *,
    route: Optional[str] = None,
) -> Optional[web.Response]:
    """403 when google_target / calendar_id / tasklist_id are outside grants. No silent coerce."""
    principal = hud_principal(request)
    if principal.get("type") != "agent":
        return None
    data = payload or {}
    grants = principal.get("grants") if isinstance(principal.get("grants"), Mapping) else principal
    allowed_targets = [
        hud_normalize_google_target(item)
        for item in (principal.get("allowed_google_targets") or [])
        if str(item).strip()
    ]
    allowed_targets = [item for item in allowed_targets if item]

    raw_target = data.get("google_target")
    if raw_target is not None and str(raw_target).strip():
        target = hud_normalize_google_target(raw_target)
        # Obsidian is HUD's primary store, not a Google grant target.
        if target and target not in {"obsidian"} and target not in allowed_targets:
            return hud_agent_forbidden_response(
                request,
                message=f"Agent is not permitted to use google_target '{target}'",
                route=route,
            )

    grant_calendar = None
    grant_tasklist = None
    if isinstance(grants, Mapping):
        grant_calendar = grants.get("calendar_id")
        grant_tasklist = grants.get("tasklist_id")
    if grant_calendar is None:
        grant_calendar = principal.get("calendar_id")
    if grant_tasklist is None:
        grant_tasklist = principal.get("tasklist_id")

    requested_calendar = data.get("calendar_id")
    if requested_calendar is not None and str(requested_calendar).strip():
        requested = str(requested_calendar).strip()
        if grant_calendar is not None and requested != str(grant_calendar).strip():
            return hud_agent_forbidden_response(
                request,
                message="Agent is not permitted to use this calendar_id",
                route=route,
            )
    requested_tasklist = data.get("tasklist_id")
    if requested_tasklist is not None and str(requested_tasklist).strip():
        requested = str(requested_tasklist).strip()
        if grant_tasklist is not None and requested != str(grant_tasklist).strip():
            return hud_agent_forbidden_response(
                request,
                message="Agent is not permitted to use this tasklist_id",
                route=route,
            )
    return None


def hud_stored_item_grant_payload(item: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Grant fields from a stored HUD item, including payload_json fallbacks."""
    if not isinstance(item, Mapping):
        return {}
    nested = item.get("payload_json")
    nested = nested if isinstance(nested, Mapping) else {}
    out: Dict[str, Any] = {}
    for key in ("role_ref", "role", "google_target", "calendar_id", "tasklist_id"):
        raw = item.get(key)
        if raw is None or (isinstance(raw, str) and not str(raw).strip()):
            raw = nested.get(key)
        if raw is not None and str(raw).strip():
            out[key] = raw
    return out


def hud_enforce_agent_stored_item(
    request: web.Request,
    item: Optional[Mapping[str, Any]],
    *,
    route: Optional[str] = None,
) -> Optional[web.Response]:
    """Re-check role_ref / Google grants against the stored item. Admin skips."""
    principal = hud_principal(request)
    if principal.get("type") != "agent":
        return None
    stored = hud_stored_item_grant_payload(item)
    if not stored:
        return None
    return hud_enforce_agent_role_ref(
        request, stored, route=route
    ) or hud_enforce_agent_google_grants(request, stored, route=route)


def hud_force_agent_google_ids(
    request: web.Request,
    payload: Mapping[str, Any],
    *,
    google_target: Any = None,
) -> Dict[str, Any]:
    """Force agent Google writes onto grant calendar_id / tasklist_id when those grants are set.

    Only applied for calendar/tasks targets so injecting ids cannot flip an Obsidian ingest
    into a Google write via worker hint inference.
    """
    principal = hud_principal(request)
    out = dict(payload)
    if principal.get("type") != "agent":
        return out
    grants = principal.get("grants") if isinstance(principal.get("grants"), Mapping) else principal
    calendar_id = grants.get("calendar_id") if isinstance(grants, Mapping) else None
    tasklist_id = grants.get("tasklist_id") if isinstance(grants, Mapping) else None
    target = hud_normalize_google_target(
        google_target if google_target is not None else out.get("google_target")
    )
    if target == "calendar" and calendar_id is not None and str(calendar_id).strip():
        out["calendar_id"] = str(calendar_id).strip()
    if target == "tasks" and tasklist_id is not None and str(tasklist_id).strip():
        out["tasklist_id"] = str(tasklist_id).strip()
    return out


def hud_strip_oauth_secrets(value: Any) -> Any:
    """Never let Google OAuth tokens leave HUD responses."""
    if isinstance(value, Mapping):
        redacted: Dict[str, Any] = {}
        for key, inner in value.items():
            lowered = str(key).lower()
            if lowered in {"access_token", "refresh_token", "id_token", "oauth_token"}:
                continue
            redacted[key] = hud_strip_oauth_secrets(inner)
        return redacted
    if isinstance(value, list):
        return [hud_strip_oauth_secrets(item) for item in value]
    return value


def hud_agent_safe_onboarding_errors() -> bool:
    return os.environ.get("HUD_AGENT_SAFE_ONBOARDING_ERRORS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }


def hud_require_post_onboarding_push_policy() -> bool:
    raw = (os.environ.get("HUD_REQUIRE_POST_ONBOARDING_PUSH") or "1").strip().lower()
    if raw in {"0", "false", "no", "off"}:
        return False
    return True


def hud_maybe_redact_onboarding_error_data(data: Mapping[str, Any]) -> Dict[str, Any]:
    if not hud_agent_safe_onboarding_errors():
        return dict(data)
    return {
        "onboarding_needed": bool(data.get("onboarding_needed", True)),
        "onboarding_complete": bool(data.get("onboarding_complete", False)),
        "next_action": str(data.get("next_action") or "onboard"),
    }


def hud_maybe_redact_push_policy_error_data(data: Mapping[str, Any]) -> Dict[str, Any]:
    if not hud_agent_safe_onboarding_errors():
        return dict(data)
    return {
        "post_onboarding_push_required": bool(
            data.get("post_onboarding_push_required", True)
        ),
        "next_action": "choose_push_policy",
    }


def hud_parse_explicit_bool(value: Any) -> Optional[bool]:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "y", "on"}:
        return True
    if text in {"0", "false", "no", "n", "off"}:
        return False
    return None


def hud_effective_projection_mode(
    request: web.Request,
    *,
    store: HUDStore,
    user_id: str,
    payload: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    requested = None
    if payload is not None:
        requested = normalize_projection_mode(payload.get("projection_mode"))
    if requested is None:
        requested = normalize_projection_mode(request.query.get("projection_mode"))
    from_store = store.get_user_projection_mode(user_id)
    effective = requested or from_store or HUD_PROJECTION_MODE_DRY_RUN
    if effective not in {HUD_PROJECTION_MODE_DRY_RUN, HUD_PROJECTION_MODE_LIVE}:
        raise ValueError("projection_mode must be 'dry_run', 'live', or 'direct' (alias for live)")
    return {
        "effective_projection_mode": effective,
        "projection_mode_requested": requested,
        "projection_mode_from_store": from_store,
    }


def hud_projection_mode_client_fields(bundle: Mapping[str, Any]) -> Dict[str, Any]:
    fields: Dict[str, Any] = {
        "effective_projection_mode": bundle.get("effective_projection_mode"),
        "projection_mode_requested": bundle.get("projection_mode_requested"),
        "projection_mode_from_store": bundle.get("projection_mode_from_store"),
    }
    pw = bundle.get("persist_warning")
    if pw:
        fields["persist_warning"] = pw
    return {k: v for k, v in fields.items() if v is not None or k == "effective_projection_mode"}


def hud_push_policy_client_fields(store: HUDStore, user_id: str) -> Dict[str, Any]:
    if not hud_require_post_onboarding_push_policy():
        return {}
    try:
        policy = store.get_user_push_policy(user_id)
    except Exception:
        logger.exception("HUD get_user_push_policy failed user_id=%s", user_id)
        policy = None
    if policy is not None:
        return {"post_onboarding_push_policy_required": False}
    return {
        "post_onboarding_push_policy_required": True,
        "next_action": "choose_push_policy",
    }


def hud_onboarding_gate_response_if_blocked(
    *,
    store: HUDStore,
    route: str,
    actor: Optional[str],
    user_id: str,
) -> Optional[web.Response]:
    from hud.onboarding_db import (
        hud_onboarding_db_status,
        is_user_onboarding_atomic_complete,
        is_user_fully_onboarded,
    )

    if is_user_fully_onboarded(store, user_id):
        return None
    if not is_user_onboarding_atomic_complete(store, user_id):
        ctx = hud_onboarding_db_status(store, user_id)
    else:
        # atomic complete but push pending: do not block here (primary gate is fully); let push gate give precise guidance
        return None
    ctx = hud_onboarding_db_status(store, user_id)
    full_data: Dict[str, Any] = {
        "onboarding_needed": True,
        "onboarding_complete": False,
        "onboarding_needed_reason": ctx,
        "next_action": "onboard",
    }
    error_data = finalize_hud_data(
        hud_maybe_redact_onboarding_error_data(full_data),
        store=store,
        user_id=user_id,
    )
    # Rich mode-aware diagnostic for the agent
    error_data["mcp_meta"] = build_rich_error_meta(
        current_mode="onboarding",
        tool="gated path",
        violation="onboarding not complete (no valid atomic record)",
        guidance="Follow ONBOARDING sequence: read template → survey user → submit full atomic in one call. 409s will indicate what is still required.",
    )

    body = hud_error_payload(
        "Complete personal context onboarding (soul.md) before this HUD operation.",
        "onboarding_required",
        "must_complete_onboarding",
        route=route,
        actor=actor,
        data=error_data,
    )
    return web.json_response(body, status=HUD_ERROR_HTTP_STATUS["onboarding_required"])


def hud_push_policy_gate_response_if_blocked(
    *,
    store: HUDStore,
    route: str,
    actor: Optional[str],
    user_id: str,
) -> Optional[web.Response]:
    from hud.onboarding_db import is_user_onboarding_atomic_complete

    if not hud_require_post_onboarding_push_policy():
        return None
    if not is_user_onboarding_atomic_complete(store, user_id):
        return None
    try:
        policy = store.get_user_push_policy(user_id)
    except Exception:
        logger.exception("HUD get_user_push_policy failed user_id=%s", user_id)
        policy = None
    if policy is not None:
        return None
    full_data: Dict[str, Any] = {
        "post_onboarding_push_required": True,
        "next_action": "choose_push_policy",
        "onboarding_needed": False,
        "onboarding_complete": True,
    }
    error_data = finalize_hud_data(
        hud_maybe_redact_push_policy_error_data(full_data),
        store=store,
        user_id=user_id,
    )
    # Rich diagnostic 409 for the agent in operational state
    error_data["mcp_meta"] = build_rich_error_meta(
        current_mode="operational",
        tool="push policy gate",
        violation="push preference not yet set after atomic onboarding",
        guidance="Ask the user for their push preference (external_push_without_approval) using a short targeted question if needed, then call hud.onboarding with it.",
    )

    body = hud_error_payload(
        "Mission profile is ready; confirm whether HUD may push to external calendar and tasks "
        "without per-item approval.",
        "policy_required",
        "must_complete_push_policy",
        route=route,
        actor=actor,
        data=error_data,
    )
    return web.json_response(body, status=HUD_ERROR_HTTP_STATUS["push_policy_required"])


def parse_projection_mode_from_payload(payload: Mapping[str, Any]) -> str:
    mode = payload.get("projection_mode")
    if mode is None:
        return HUD_PROJECTION_MODE_DRY_RUN
    return parse_projection_mode(mode)
